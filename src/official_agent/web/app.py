"""客服 Agent FastAPI 服务(INF-04):官网候选人只读问答通道。

分层:
- 本模块:FastAPI app + lifespan(checkpointer 生命周期)+ 健康检查 + CORS
- routes.py:业务路由(`/api/agent/chat` SSE)与会话管理
- graphs/identity.resolve 的 kind=="web" 分支:官网 JWT → /auth/me 换身份(#89 A2,
  已落地真实端点;解析失败由路由返回 401)

与 CLI(INF-03)复用同一套装配:build_assistant_agent / langfuse_callbacks /
get_checkpointer / threads 建档 —— agent 进程内直连工具函数,不走 MCP 回环(ADR-0003)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
            # L-1:幂等建 agent_threads + agent_conversation_log + agent_config 表
            # (SEC-07 / M6 #110 / #111)。缺表时降级(fail-open,ADR-0005)。
            ensure_agent_threads_table()
            conversation.ensure_conversation_table()
            config_store.ensure_config_table()
        except Exception:  # noqa: BLE001 — PG 未起/配置错 → 降级(fail-open,ADR-0005)
            app.state.checkpointer = None
        yield


def create_app() -> FastAPI:
    """构建 FastAPI app。uvicorn 入口:``uvicorn official_agent.web.app:create_app``
    (factory 模式,便于测试注入)。"""
    settings = get_settings()
    app = FastAPI(title="official-web-agent", version="0.1.0", lifespan=lifespan)

    origins = [o.strip() for o in settings.agent_cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from official_agent.web import routes

    app.include_router(routes.router, prefix="/api/agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        """探活(不鉴权)。checkpointer 就绪(PG 连通)才算健康。"""
        ready = getattr(app.state, "checkpointer", None) is not None
        return {"status": "ok" if ready else "degraded"}
    return app
