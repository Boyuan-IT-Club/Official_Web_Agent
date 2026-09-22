"""客服 Agent FastAPI 服务:官网候选人只读问答通道。

分层:
- 本模块:FastAPI app + lifespan(checkpointer 生命周期)+ 健康检查 + CORS
- routes.py:业务路由(`/api/agent/chat` SSE)与会话管理
- graphs/identity.resolve 的 kind=="web" 分支:官网 JWT → /auth/me 换身份
  (已落地真实端点;解析失败由路由返回 401)

与 CLI 复用同一套装配:build_assistant_agent / langfuse_callbacks /
get_checkpointer / threads 建档 —— agent 进程内直连工具函数,不走 MCP 回环(ADR-0003)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from official_agent.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """服务生命周期:checkpointer 连接随进程,会话态在 checkpointer 不在进程。

    AsyncPostgresSaver 是跨请求共享的连接(checkpointer 无会话态,thread_id 隔离会话),
    get_checkpointer() 进入建连+建表,退出关闭(与 CLI cli.py:169-176 同语义)。
    """
    from official_agent.state import config_store, conversation
    from official_agent.state.pg import get_checkpointer
    from official_agent.state.threads import ensure_agent_threads_table

    async with get_checkpointer() as saver:
        app.state.checkpointer = saver
        try:
            # 幂等建 agent_threads + agent_conversation_log + agent_config 表。
            # 缺表时降级(fail-open,ADR-0005)。
            ensure_agent_threads_table()
            conversation.ensure_conversation_table()
            config_store.ensure_config_table()
            # 纯 web 部署也要有审计面(管理员原文读取审计依赖)
            from official_agent.state.audit import ensure_audit_table

            ensure_audit_table()
            # 评测三表(scorecard / qbank / job)的自举全在这里:建表 DDL
            # 即使 no-op 也持表锁,留在数据路径上会把读写串行化
            # (见 state/evaluation.py 顶部的自举注释)。job 表还要去重
            # legacy 重复活跃行 + 建部分唯一索引,必须先于下面的启动恢复。
            from official_agent.state.evaluation import (
                ensure_evaluation_job_ready,
                ensure_evaluation_scorecard_ready,
            )
            from official_agent.state.qbank import ensure_qbank_ready

            ensure_evaluation_scorecard_ready()
            ensure_qbank_ready()
            ensure_evaluation_job_ready()
        except Exception:  # noqa: BLE001 — PG 未起/配置错 → 降级(fail-open,ADR-0005)
            # 降级必须留痕:配置错误与 PG 未起症状相同(/health degraded),靠这条日志区分
            logging.getLogger(__name__).warning(
                "启动建表自举失败,降级为无 checkpointer(记忆/落账不可用)", exc_info=True
            )
            app.state.checkpointer = None
        # 启动自动恢复:进程重启后,PG 里残留的 pending/running job
        # 由 lifespan 全量扫回并重派(超 10 分钟 + attempts 未满;达上限的
        # 转 failed 交人工)。失败 fail-open——PG 未起时跳过,下次重启再恢复。
        try:
            from official_agent.evaluation.runner import get_runner

            await get_runner().recover_stale_on_startup()
        except Exception:  # noqa: BLE001 — 恢复失败不挡服务启动(ADR-0005 fail-open)
            logging.getLogger(__name__).warning(
                "启动自动恢复失败,残留 job 留待下次/手动重试", exc_info=True
            )

        # 会话 TTL 清理 job——软删档案超保留期(连带 checkpoint/对话日志)
        # 物理清理,每 6h 一轮,fail-open。thread_retention_days=0 时为 no-op:
        # 保留天数由数据留存 ADR 决定,ADR 落地前不启用。
        ttl_stop = asyncio.Event()

        async def _session_ttl_loop() -> None:
            from official_agent.config import get_effective_settings
            from official_agent.state.conversation import delete_thread_conversations
            from official_agent.state.pg import purge_thread_checkpoints
            from official_agent.state.threads import (
                hard_delete_thread,
                list_expired_soft_deleted,
            )

            while not ttl_stop.is_set():
                with contextlib.suppress(Exception):
                    days = int(get_effective_settings().thread_retention_days)
                    expired = await asyncio.to_thread(list_expired_soft_deleted, days)
                    for tid in expired:
                        with contextlib.suppress(Exception):
                            await asyncio.to_thread(purge_thread_checkpoints, tid)
                            await asyncio.to_thread(delete_thread_conversations, tid)
                            await asyncio.to_thread(hard_delete_thread, tid, owner_user_id=None)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ttl_stop.wait(), timeout=6 * 3600)

        ttl_task = asyncio.create_task(_session_ttl_loop())

        # 挂起载荷 24h TTL 清理 job(每 6h 一轮,fail-open)
        purge_stop = asyncio.Event()

        async def _purge_loop() -> None:
            from official_agent.state.pg import purge_expired_interrupts

            while not purge_stop.is_set():
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(purge_expired_interrupts, max_age_hours=24)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(purge_stop.wait(), timeout=6 * 3600)

        purge_task = asyncio.create_task(_purge_loop())
        try:
            yield
        finally:
            ttl_stop.set()
            ttl_task.cancel()
            purge_stop.set()
            purge_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await ttl_task
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await purge_task


def create_app() -> FastAPI:
    """构建 FastAPI app。uvicorn 入口:``uvicorn official_agent.web.app:create_app``
    (factory 模式,便于测试注入)。"""
    settings = get_settings()
    # 日志 stdout + 落盘 RotatingFileHandler(幂等,测试安全)
    from official_agent.logging_conf import setup_logging

    setup_logging()
    app = FastAPI(title="official-web-agent", version="0.1.0", lifespan=lifespan)

    origins = [o.strip() for o in settings.agent_cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _request_trace_id(request: Request, call_next):
        """入口置请求级 trace id,响应头 X-Request-Id 回传。

        入站 X-Request-Id 优先(上游/网关串联),否则随机;值统一归一为
        W3C 32-hex,此后日志(TraceIdFilter)、出站后端请求 traceparent、
        审计 trace_id 共用同一 id——一次请求全线可按 id 串成一条线。
        """
        import secrets

        from official_agent.observability import (
            reset_turn_trace_id,
            set_turn_trace_id,
            to_w3c_trace_id,
        )

        rid = to_w3c_trace_id(request.headers.get("x-request-id") or secrets.token_hex(16))
        token = set_turn_trace_id(rid)
        try:
            response = await call_next(request)
        finally:
            reset_turn_trace_id(token)
        response.headers["X-Request-Id"] = rid
        return response

    from official_agent.web import routes
    from official_agent.web.config_admin import router as config_admin_router
    from official_agent.web.kb_admin import router as kb_admin_router

    app.include_router(routes.router, prefix="/api/agent")
    app.include_router(config_admin_router, prefix="/api/agent")
    app.include_router(kb_admin_router, prefix="/api/agent")

    from official_agent.web.evaluation_admin import router as evaluation_admin_router

    app.include_router(evaluation_admin_router, prefix="/api/agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        """探活(不鉴权)。checkpointer 就绪(PG 连通)才算健康。"""
        ready = getattr(app.state, "checkpointer", None) is not None
        return {"status": "ok" if ready else "degraded"}

    return app
