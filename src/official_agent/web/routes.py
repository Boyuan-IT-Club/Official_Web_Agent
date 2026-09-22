"""客服 Agent SSE 聊天路由。

会话模型:
- 会话 = thread_id(格式 web:u{user}:{rand8}),首次 POST 建档并返回给前端;
  续传带原 session_id(= thread_id)。会话记忆在共享 checkpointer(thread_id 维度),
  不在进程对象 —— 进程重启后同 session_id 可重建 agent 并续上下文。
- agent 按「身份 × user_token」装配(assemble_tools 把官网 JWT 闭包绑定到
  get_my_interview,数据查询 JWT 直转),故每会话持有一个 agent;
  待工具改为从图 state 取 token 后可共享单 graph。
- 权限:身份经 resolve(kind=web,调后端 /auth/me 换身份;官网通道唯一入口)。
  身份解析失败 → 401;不放开匿名/模拟身份进生产路径。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from pydantic import BaseModel

from official_agent.graphs.assistant import (
    _ROLE_TOOL_NAMES,  # noqa: PLC2701 — 同模块装配表(prefix_hash 证据)
    build_assistant_agent,
    tool_roster,
)
from official_agent.graphs.assistant.compression import (
    maybe_compress,
    summarize_messages,
)
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.observability import langfuse_callbacks
from official_agent.security.pii import ReplyPiiMasker
from official_agent.state.threads import create_thread, new_thread_id, resolve_thread
from official_agent.tools.client import BackendError, BackendUnavailableError
from official_agent.tools.readonly import asker_scope
from official_agent.web import agent_factory, session_store, telemetry
from official_agent.web.auth import authenticate as _authenticate
from official_agent.web.auth import require_any as _require_any

router = APIRouter()

logger = logging.getLogger(__name__)

# 恢复拒绝的统一文案:不存在/跨属主/已终结不区分(防会话枚举翻看)
_SESSION_RESUME_REJECT = "会话不存在、已结束或无权访问"




async def _get_or_create_session(
    request: Request,
    identity: ResolvedIdentity,
    user_token: str,
    session_id: str | None,
) -> tuple[session_store.SessionState, bool]:
    """取会话;无则(或未给)新建并建档。同一 session_id 只能被同 user 续传。

    返回 (session, is_new):is_new=True 表示本会话是进程内新建(首轮须注身份前缀),
    False 表示续传既有会话。不依赖 aget_state(LangGraph 对无 checkpoint 的 thread
    可能返回非 None,导致 is_new 恒 False,身份永不在首轮注入——实测坑)。

    重启续聊:内存未命中但显式携带 session_id(进程重启/LRU 淘汰后重入)
    → resolve_thread 做属主与 active 校验;通过则以原 thread_id 重建运行时 agent,
    上下文从共享 PG checkpointer 续读,is_new=False(身份早已注入,幂等兜底仍在);
    跨属主/已终结/不存在统一 404,档案校验故障 503 fail-closed——绝不静默换新会话。
    """
    now = time.monotonic()
    async with session_store.lock:
        # 删除中的会话一律拒绝——先于内存命中/档案恢复两条分支。
        # 删除端在持锁登记后立刻放锁去做磁盘清理;此处若放行,新轮次会与
        # 清理并发读写同一 checkpoint thread(要么复活已删数据,要么读到半删状态)。
        if session_id and session_id in session_store.deleting:
            raise HTTPException(status_code=409, detail="会话正在删除中")
        if session_id and session_id in session_store.sessions:
            existing = session_store.sessions[session_id]
            if existing.identity.get("user_id") != identity.get("user_id"):
                raise HTTPException(status_code=403, detail="无权访问该会话")
            # 续传但 token 变了(官网 JWT 轮换/过期重登):重建 agent 绑定新 token,
            # thread_id 不变(记忆在 checkpointer,agent 无会话态,ADR-0006 token 有生命周期)。
            if existing.user_token != user_token:
                checkpointer = getattr(request.app.state, "checkpointer", None)
                existing.agent = build_assistant_agent(
                    identity, user_token=user_token, checkpointer=checkpointer
                )
                existing.user_token = user_token
                existing.identity = identity
            session_store.sessions.move_to_end(session_id)
            session_store.last_access[session_id] = now
            return existing, False

        user_id = identity.get("user_id")

        # 重启/淘汰后的恢复路径——显式 session_id 必先过档案属主校验
        if session_id:
            if user_id is None:
                raise HTTPException(status_code=404, detail=_SESSION_RESUME_REJECT)
            try:
                rec = await asyncio.to_thread(resolve_thread, session_id, int(user_id))
            except Exception as exc:  # noqa: BLE001 — 校验故障不得静默换新会话
                raise HTTPException(status_code=503, detail="会话恢复校验失败,请稍后重试") from exc
            if rec is None:
                raise HTTPException(status_code=404, detail=_SESSION_RESUME_REJECT)
            # 恢复既有会话必须有 checkpointer——档案存在但
            # checkpointer 不可用(进程内 PG 故障)时,返回 is_new=False 会让
            # 用户以为「续聊成功」而实际上下文为空。宁可 503 也不静默降级。
            # (新建会话仍允许 fail-open 降级:无历史可丢,见下方分支。)
            checkpointer = getattr(request.app.state, "checkpointer", None)
            if checkpointer is None:
                raise HTTPException(
                    status_code=503, detail="会话恢复暂时不可用,请稍后重试"
                )
            agent = build_assistant_agent(
                identity, user_token=user_token, checkpointer=checkpointer
            )
            session = session_store.SessionState(session_id, identity, user_token, agent)
            session.applied_config_fingerprint = await asyncio.to_thread(
                agent_factory.config_fingerprint
            )
            session_store.sessions[session_id] = session
            session_store.last_access[session_id] = now
            session_store.evict_locked(now)
            return session, False

        # thread_id:建档优先;PG 不可用降级随机 thread_id(保隔离,不持久化)
        try:
            if user_id is not None:
                # psycopg 是同步驱动,建档不能在事件循环上直跑(此处还持 session_store.lock)
                rec = await asyncio.to_thread(create_thread, "web", user_id, subject="web-chat")
                session_id = rec.thread_id
            else:
                session_id = new_thread_id("web", 0)
        except Exception:  # noqa: BLE001 — 建档失败不阻断对话(与 CLI 同语义)
            # 降级必须留痕:否则 conversation 档案静默丢失,排障无从下手
            logger.warning("会话建档失败,降级为无档案随机会话", exc_info=True)
            session_id = new_thread_id("web", user_id or 0)

        # checkpointer:进程级共享(app lifespan 建立,fail-open)。thread_id 隔离会话。
        checkpointer = getattr(request.app.state, "checkpointer", None)
        agent = build_assistant_agent(identity, user_token=user_token, checkpointer=checkpointer)
        session = session_store.SessionState(session_id, identity, user_token, agent)
        # 新建即记录当前配置指纹,避免首轮热生效比对误重建
        session.applied_config_fingerprint = await asyncio.to_thread(
            agent_factory.config_fingerprint
        )
        session_store.sessions[session_id] = session
        session_store.last_access[session_id] = now
        session_store.evict_locked(now)
        return session, True


class ChatBody(BaseModel):
    """POST /chat 入参:一轮用户消息。session_id 缺省 = 新建会话。

    形状校验交 schema(畸形 JSON → FastAPI 标准 422);空/超长属语义
    校验,端点内手工判(保持 400 契约,前端文案不变)。
    """

    message: str
    session_id: str | None = None


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatBody,
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> StreamingResponse:
    """一轮对话(SSE 流)。body: {"message": str, "session_id": str | null}

    首次(session_id 空)→ 服务端生成 session_id 并随流返回;续传带原 session_id。
    SSE 事件(data 为 JSON):session(带 created) / delta(role,content) /
    tool(role,name) / done / error(code,message)。
    """
    identity, user_token = auth
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")
    if len(message) > _MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail=f"消息过长(上限 {_MAX_MESSAGE_CHARS} 字)")
    session_id = (body.session_id or "").strip() or None

    session, is_new = await _get_or_create_session(request, identity, user_token, session_id)
    checkpointer = getattr(request.app.state, "checkpointer", None)
    return StreamingResponse(
        _stream_turn(session, message, is_new, checkpointer),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# SSE 流内 error 事件 code(面向客户端的稳定错误码)。前端按 code 决定动作。
_ERR_AUTH_EXPIRED = "auth_expired"
_ERR_BACKEND_UNAVAILABLE = "backend_unavailable"
_ERR_MODEL = "model_error"
_ERR_INVALID_REQUEST = "invalid_request"
_ERR_UNKNOWN = "unknown"
# 客户端断连中止(CancelledError):不在客户端错误码契约内,运营观测专用——断连轮次留痕
_ERR_DISCONNECTED = "client_disconnected"
# 同会话并发轮次占用:第二个并发请求立即拒绝,不入队
_ERR_BUSY = "busy"
# 单轮墙钟超时中止
_ERR_TIMEOUT = "timeout"

# 面向客户端的稳定错误文案(不含原始异常/内部细节),附 trace_id 供排障。
# 完整异常只进服务端日志(logger.warning exc_info)。
_ERR_SAFE_COPY: dict[str, str] = {
    _ERR_BUSY: "上一条消息还在回复中,请稍候",
    _ERR_AUTH_EXPIRED: "登录状态已过期,请重新登录后再试",
    _ERR_BACKEND_UNAVAILABLE: "服务暂时不可用,请稍后重试",
    _ERR_MODEL: "模型服务出现异常,请稍后重试",
    _ERR_INVALID_REQUEST: "这条消息无法处理,请调整后重试",
    _ERR_TIMEOUT: "本轮响应超时已中止,请稍后重试",
    _ERR_UNKNOWN: "服务出现异常,请稍后重试",
}

# 单条消息长度上限(先收敛单请求滥用面;超限走 400 invalid_request,前端零改动)
_MAX_MESSAGE_CHARS = 2000

# 全局活跃模型调用并发闸(跨用户资源保护;进程内,数值待定)
_model_gate: asyncio.Semaphore | None = None


async def _get_model_gate() -> asyncio.Semaphore:
    global _model_gate
    if _model_gate is None:
        from official_agent.config import get_effective_settings

        # get_effective_settings 每次直连 PG 读 agent_config(无缓存),不能占事件循环;
        # 闸建好后进程内复用,不再触库
        settings = await asyncio.to_thread(get_effective_settings)
        _model_gate = asyncio.Semaphore(max(int(settings.model_call_global_concurrency), 1))
    return _model_gate


def _sse_error(code: str) -> dict[str, str]:
    """稳定错误事件:安全文案 + trace_id,绝不带原始异常串。"""
    from official_agent.observability import current_trace_id

    copy = _ERR_SAFE_COPY.get(code) or _ERR_SAFE_COPY[_ERR_UNKNOWN]
    return {"type": "error", "code": code, "message": f"{copy}(trace:{current_trace_id()})"}


def _error_code(exc: Exception) -> str:
    """执行期异常 → 面向客户端的稳定错误码。分类原则:
    - BackendAuthError(用户令牌/服务账号凭证失效)→ auth_expired
    - BackendUnavailableError(网络/传输故障)→ backend_unavailable
    - httpx 传输/超时 → backend_unavailable(后端不可达/网关错)
    - 其余 BackendError(业务错误)按其文案;未知 → unknown
    观测/模型错误由 LangGraph 包装,不易精确识别,归 unknown(前端可重试)。
    分类只看异常类型,不看文案——后端文案一改,子串匹配的分类立刻失效。
    """
    import httpx

    from official_agent.tools.client import BackendAuthError

    if isinstance(exc, BackendAuthError):
        return _ERR_AUTH_EXPIRED
    if isinstance(exc, (httpx.HTTPError, BackendUnavailableError)):
        return _ERR_BACKEND_UNAVAILABLE
    if isinstance(exc, BackendError):
        # 业务错误(如「未投递」)不是系统故障——按 invalid_request 让前端展示 message
        return _ERR_INVALID_REQUEST
    return _ERR_UNKNOWN


async def _compress_if_needed(
    session: session_store.SessionState, config: dict, user_query: str
) -> str | None:
    """轮末检查会话 token,超阈值则压缩回写 checkpoint。

    回写 = update_state 产生 checkpoint **新版本**(先 REMOVE_ALL_MESSAGES
    再加「摘要 + 近几轮」);PostgresSaver 不删旧版本行,全量历史仍可
    get_state_history 回溯——checkpointer 始终是对话原文权威源。
    返回事件描述串(触发轮/token/覆盖/保留/摘要体积)随 conversation_log
    落行;未触发或任何失败返回 None(fail-open,ADR-0005)。
    熔断(ADR-0004):连续失败达阈值后暂停压缩尝试,降级为不压缩。
    """
    from official_agent.config import get_effective_settings
    from official_agent.graphs.assistant import build_model
    from official_agent.graphs.assistant.compression import (
        SUMMARY_MAX_TOKENS,
        compression_paused,
        record_compression_failure,
        record_compression_success,
    )

    if compression_paused():  # 熔断中:降级为不压缩,不再打扰主链路
        return None
    try:
        state = await session.agent.aget_state(config)
        messages = (getattr(state, "values", None) or {}).get("messages") or []
        if not messages:
            return None
        settings = await asyncio.to_thread(get_effective_settings)
        # 摘要用 strong 模型(ADR-0004「压缩即理解」,降 light 须 eval 证明);
        # 温度 0 + 输出预算 = reasoning-safe
        summarizer = build_model(settings).bind(temperature=0, max_tokens=SUMMARY_MAX_TOKENS)
        result = await maybe_compress(
            messages,
            summarize_fn=lambda older, query: summarize_messages(older, query, summarizer),
            threshold=settings.context_compress_threshold_tokens,
            recent_keep=settings.context_recent_keep_messages,
            query=user_query,
        )
        if result is None:
            return None
        await session.agent.update_state(
            config,
            {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result.new_messages]},
        )
        record_compression_success()
        return (
            f"turn={session.turns};trigger_tokens={result.trigger_tokens};"
            f"covered={result.covered};kept={len(result.new_messages) - 1};"
            f"summary_tokens={result.summary_tokens}"
        )
    except Exception:  # noqa: BLE001 — 压缩失败不拖垮对话(ADR-0005)
        record_compression_failure()
        logger.warning("会话压缩失败(已忽略)", exc_info=True)
        return None


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@dataclass
class _TurnAccum:
    """单轮流式消费的就地累计状态(回复块/工具/引用锚/跨步 usage 求和)。"""

    reply_chunks: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    seen_sources: set[str] = field(default_factory=set)
    usage: dict[str, int | None] = field(
        default_factory=lambda: dict.fromkeys(
            ("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens"), 0
        )
    )


def _feed_text_chunk(
    chunk: AIMessageChunk, acc: _TurnAccum, masker: ReplyPiiMasker | None
) -> str | None:
    """文本块入回复累计,返回掩码后可下发片段(无文本/无增量 → None)。"""
    text = chunk.content if isinstance(chunk.content, str) else ""
    if not text:
        return None
    acc.reply_chunks.append(text)
    if masker is None:
        return None
    return masker.feed(text)


def _accumulate_usage(
    chunk: Any,
    usage_acc: dict[str, int | None],
    last_usage: dict[str, int | None] | None,
) -> dict[str, int | None] | None:
    """usage 终块并入跨步累计;返回新的 last_usage。

    同值跳过:流式下同一累计值会在多个 chunk 上重复出现,不去重就重复求和。
    raw token_usage 优先(prompt_cache 字段只在原始形状)。
    """
    um = getattr(chunk, "usage_metadata", None)
    raw_usage = (getattr(chunk, "response_metadata", None) or {}).get("token_usage")
    usage_payload = raw_usage if raw_usage else um
    if um is None or not usage_payload:
        return last_usage
    extracted = telemetry.extract_usage(usage_payload)
    if extracted == last_usage:
        return last_usage
    for k, v in extracted.items():
        if v is not None:
            usage_acc[k] = (usage_acc.get(k) or 0) + v
    return extracted


def _drain_updates(payload: Any, acc: _TurnAccum) -> list[dict[str, Any]]:
    """updates 模式:收集 tool 事件载荷与 search_knowledge 引用锚。"""
    events: list[dict[str, Any]] = []
    for _ns, node_update in payload.items():
        if not isinstance(node_update, dict):
            continue
        for m in node_update.get("messages") or []:
            # 工具调用状态(tool 事件,role=tool)
            for tc in getattr(m, "tool_calls", None) or ():
                acc.tools_called.append(tc.get("name") or "")
                events.append({"type": "tool", "role": "tool", "name": tc.get("name")})
            # search_knowledge 的结果收集为引用锚(轮末随 delta.sources 下发)
            if isinstance(m, ToolMessage) and getattr(m, "name", "") == "search_knowledge":
                telemetry.collect_sources(m, acc.sources, acc.seen_sources)
    return events


async def _consume_model_stream(
    agent: Any,
    messages: list,
    config: dict,
    *,
    masker: ReplyPiiMasker | None,
    acc: _TurnAccum,
) -> AsyncIterator[str]:
    """消费 astream 双模式流:吐 SSE 事件,就地更新 acc(回复/工具/引用/usage)。"""
    last_usage: dict[str, int | None] | None = None
    async for mode, payload in agent.astream(
        {"messages": messages}, config=config, stream_mode=["messages", "updates"]
    ):
        if mode == "messages":
            chunk, _meta = payload
            if isinstance(chunk, AIMessageChunk):
                out = _feed_text_chunk(chunk, acc, masker)
                if out:
                    yield _sse({"type": "delta", "role": "assistant", "content": out})
            last_usage = _accumulate_usage(chunk, acc.usage, last_usage)
        elif mode == "updates":
            for event in _drain_updates(payload, acc):
                yield _sse(event)


async def _toolless_guarded_reply(
    agent: Any, config: dict, reply_chunks: list[str]
) -> AsyncIterator[str]:
    """无工具档:整段回复过编造守卫后一次性下发(缓冲直播)。

    守卫在一切持久化之前——直播、conversation_log、checkpointer 三面同用
    改写文本;压缩必须排在其后,否则编造原文会被压进摘要。
    """
    from official_agent.security.fabrication_guard import guard_empty_tools_reply
    from official_agent.security.pii import mask_pii_output

    final_reply, verdict = guard_empty_tools_reply("".join(reply_chunks))
    # toolless 回复出口同过 PII 掩(与 cli 对称;掩后文本进回写)
    final_reply, _pii_trace = mask_pii_output(final_reply)
    if verdict != "clean":
        logging.getLogger(__name__).warning(
            "guard_event guard_name=%s verdict=%s tool=<(empty)>",
            "fabrication_empty_tools",
            verdict,
        )
        reply_chunks[:] = [final_reply]
        await _rewrite_last_ai_message(agent, config, final_reply)
    if final_reply:
        yield _sse({"type": "delta", "role": "assistant", "content": final_reply})


def _usage_out(usage_acc: dict[str, int | None]) -> dict[str, int | None]:
    """全零(无 usage 数据)→ 全 None 形状(落账列语义:未采到 ≠ 0)。"""
    if any(usage_acc.values()):
        return usage_acc
    return {k: None for k in usage_acc}


async def _stream_turn(
    session: session_store.SessionState, message: str, is_new: bool, checkpointer: Any = None
) -> AsyncIterator[str]:
    """跑一轮:流式吐 SSE。is_new 仅作 SSE session 事件的 created 标记。

    编排序:锁 → 热生效 → session 事件 → 受闸流消费 → 守卫/尾冲 → 压缩 →
    落账 → sources/done(或 error)。每轮结束在 conversation_log 落一行:
    正常存问题原文+回复摘要+tools/耗时;异常只存 error_code+耗时,不存内容。
    落行失败(fail-open)不阻断对话——观测绝不拖垮主流程(ADR-0005)。
    """

    # 同会话并发轮次串行化——锁被占用时立即回 busy,
    # 不排队(前端提示「上一条还在回复中」);check/acquire 间无 await,原子。
    if session.turn_lock.locked():
        yield _sse({"type": "error", "code": _ERR_BUSY, "message": "上一条消息还在回复中,请稍候"})
        return
    await session.turn_lock.acquire()
    try:
        # 配置热生效:配置指纹变了 → 重建 agent(新 model/provider 立即作用于本轮)
        await agent_factory.ensure_fresh_agent(
            session, checkpointer=checkpointer, build_agent=build_assistant_agent
        )
        # 轮计数(1 起),压缩事件「触发轮」留痕用
        session.turns += 1

        config: dict[str, Any] = {
            "configurable": {"thread_id": session.session_id},
            "callbacks": langfuse_callbacks(),
            # Langfuse trace 尚不能按 thread 删除,这里把稳定 correlation 键
            # 写入 trace metadata,为后续按 thread 删除/短 TTL 提供可执行索引;
            # 删除闭环留下的残余 trace 因此有据可查。
            "metadata": {
                "thread_id": session.session_id,
                "user_id": session.identity.get("user_id"),
                "channel": session.identity.get("source") or "web",
            },
        }

        # 身份/权限上下文已移入 system prompt(build_system_prompt),
        # 不再作为首条用户消息注入——checkpointer 不再存内部指令,历史回看
        # 不会把它渲染成用户气泡。直接以用户原文开轮。
        messages: list = [HumanMessage(content=message)]

        # 首事件 session(带 created 标记新/续传)
        yield _sse({"type": "session", "session_id": session.session_id, "created": is_new})

        started = time.monotonic()
        acc = _TurnAccum()
        # 无工具档缓冲整段回复,流尾过编造守卫后一次性下发
        toolless = not tool_roster(session.identity)
        # 流式 delta 逐块过 PII 掩码器(尾部缓冲抗跨块)
        masker = None if toolless else ReplyPiiMasker()
        error_code: str | None = None
        # 只读查询以来问者本人 JWT 执行(ADR-0006「社团官网层问答助手」):
        # 本轮内 readonly 查询经 _read 走 get_as_user,后端按本人权限判+归因
        async with asker_scope(session.user_token):
            # 全局活跃模型调用闸(跨用户资源保护)+ 单轮墙钟超时——
            # 卡死的模型/工具调用在配置时限内被取消,turn_lock 随 with 释放。
            from official_agent.config import get_effective_settings

            _settings = await asyncio.to_thread(get_effective_settings)
            config["recursion_limit"] = max(int(_settings.turn_recursion_limit), 1)
            gate = await _get_model_gate()
            try:
                gate_acquired = False
                try:
                    await asyncio.wait_for(
                        gate.acquire(),
                        timeout=max(int(_settings.model_gate_acquire_timeout), 1),
                    )
                    gate_acquired = True
                except TimeoutError:
                    # 闸满不提前 return——闸满也是「一轮失败」,必须走统一落账
                    # 路径,否则资源饱和事件从 conversation_log 消失(收尾点
                    # 唯一:正常/错误/超时/gate-busy/断连五条路径都落一行)。
                    error_code = _ERR_BUSY
                    logger.warning(
                        "chat turn gate busy session=%s turns=%d concurrency=%d",
                        session.session_id,
                        session.turns,
                        _settings.model_call_global_concurrency,
                    )
                else:
                    async with asyncio.timeout(max(int(_settings.turn_wall_clock_timeout), 1)):
                        async for event in _consume_model_stream(
                            session.agent, messages, config, masker=masker, acc=acc
                        ):
                            yield event
            except asyncio.CancelledError:
                # 客户端断连(CancelledError 非 Exception):中止轮次也要留痕
                # (partial reply/已调工具不丢失),error_code 记断连中止。
                error_code = _ERR_DISCONNECTED
            except TimeoutError:
                # 墙钟超时——取消下游、锁随 with 释放、不压缩失败轮次;
                # 客户端只收稳定文案,完整异常服务端留痕。
                error_code = _ERR_TIMEOUT
                logger.warning(
                    "chat turn timeout session=%s turns=%d usage=%s",
                    session.session_id,
                    session.turns,
                    acc.usage,
                )
            except Exception as exc:  # noqa: BLE001 — 单轮失败不崩连接,吐 error 事件
                error_code = _error_code(exc)
                logger.warning(
                    "chat turn failed session=%s code=%s",
                    session.session_id,
                    error_code,
                    exc_info=True,
                )
            finally:
                if gate_acquired:
                    gate.release()

        from official_agent.graphs.assistant import build_system_prompt

        if toolless and error_code is None:
            async for event in _toolless_guarded_reply(session.agent, config, acc.reply_chunks):
                yield event

        # 回复出口 PII 守卫——全部会话适用。流式 delta 已逐块掩(尾部缓冲抗
        # 跨块),此处冲洗尾缓冲;命中即 trace(guard_event)。
        if masker:
            tail = masker.finish()
            if tail:
                yield _sse({"type": "delta", "role": "assistant", "content": tail})

        # 轮末按需压缩(先压缩后落行,同一行携带 compress_event)。
        # 在 done 事件前执行:失败 fail-open 返回 None,不阻断 done。
        # 失败/超时/断连轮次不压缩——失败轮的残缺上下文不值得进摘要。
        compress_event = None
        if error_code is None:
            compress_event = await _compress_if_needed(session, config, message)
        role = session.identity.get("role") or "unknown"
        tool_names = list(_ROLE_TOOL_NAMES.get(role, ()))
        p_hash = telemetry.prefix_hash(build_system_prompt(session.identity), tool_names)
        telemetry.log_conversation(
            session,
            user_message=message,
            reply_summary="".join(acc.reply_chunks),
            tools=acc.tools_called,
            duration_ms=_elapsed_ms(started),
            error_code=error_code,
            usage=_usage_out(acc.usage),
            prefix_hash=p_hash,
            compress_event=compress_event,
        )
        if error_code is None:
            if acc.sources:
                # 引用锚契约:delta 上新增可选 sources,不改消息 type 枚举——
                # 旧消费者收到空 content 追加无感;新前端做 [n] → 来源映射
                yield _sse(
                    {
                        "type": "delta",
                        "role": "assistant",
                        "content": "",
                        "sources": acc.sources,
                    }
                )
            yield _sse({"type": "done", "session_id": session.session_id})
        elif error_code != _ERR_DISCONNECTED:
            # 错误事件统一走稳定文案 + trace_id(原始异常只留服务端日志);
            # 断连无需事件(客户端已不在)。
            yield _sse(_sse_error(error_code))
    finally:
        session.turn_lock.release()


async def _rewrite_last_ai_message(agent: Any, config: dict, final_reply: str) -> None:
    """编造守卫回写:checkpointer 里最后一条 AI 消息替换为改写文本。

    覆盖三条持久化面的 checkpointer 一支:管理端/用户回看不再返回编造原文,
    下一轮模型上下文也不再反向强化被改写的回复。无 checkpointer(纯内存)时
    get_state 无消息,自然跳过;任何失败 fail-open 只告警(ADR-0005)。
    """
    import logging

    from langchain_core.messages import AIMessage, RemoveMessage

    try:
        state = await agent.aget_state(config)
        msgs = (state.values or {}).get("messages") or []
        last = msgs[-1] if msgs else None
        if last is None or not getattr(last, "content", ""):
            return
        if not getattr(last, "id", None):
            return  # RemoveMessage 按 id 匹配,空 id 会只增不删(编造原文留存)
        await agent.aupdate_state(
            config, {"messages": [RemoveMessage(id=last.id), AIMessage(content=final_reply)]}
        )
    except Exception:  # noqa: BLE001 — 回写失败不阻断 done
        logging.getLogger(__name__).warning("编造守卫回写 checkpointer 失败(已忽略)", exc_info=True)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# 管理 API 认证:官网 JWT → resolve → permission_codes 含 agent:monitor
_require_monitor = _require_any("agent:monitor")


# ── 管理 API:运营视图(对话列表/详情) ───────────────────────────


def list_conversations(**kwargs: Any) -> list[dict[str, Any]]:
    """运营列表(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.conversation import list_conversations as _impl

    return _impl(**kwargs)


def get_conversation(conversation_id: int) -> dict[str, Any] | None:
    """对话详情(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.conversation import get_conversation as _impl

    return _impl(conversation_id)


@router.get("/admin/conversations")
async def get_admin_conversations(
    request: Request,
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """运营列表:时间/用户/问题首字/状态,按 user_id / thread_id 过滤 + 分页。"""
    user_id_raw = request.query_params.get("user_id")
    thread_id = (request.query_params.get("thread_id") or "").strip() or None
    limit_raw = request.query_params.get("limit", "50")
    offset_raw = request.query_params.get("offset", "0")
    try:
        user_id = int(user_id_raw) if user_id_raw is not None else None
        limit = max(1, min(int(limit_raw), 200))
        offset = max(0, int(offset_raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id/limit/offset 必须为整数") from None
    items = await asyncio.to_thread(
        list_conversations, user_id=user_id, thread_id=thread_id, limit=limit, offset=offset
    )
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/admin/conversations/{conversation_id}")
async def get_admin_conversation_detail(
    conversation_id: int,
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """对话详情:轮次/工具/耗时/错误码 + 可展开回复摘要。"""
    row = await asyncio.to_thread(get_conversation, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


# ── 会话管理:用户历史会话/回看,管理员按用户查看 ────────────


def _project_messages(raw_messages: list) -> list[dict[str, str]]:
    """checkpointer 消息 → [{role, content}]:只保留 user/assistant 文本。

    工具调用中间态(tool/system/空内容)不进回看面——原文回看是给「人读对话」,
    不是调试轨迹(轨迹走 Langfuse)。
    """
    out: list[dict[str, str]] = []
    for m in raw_messages or []:
        role = getattr(m, "type", None)
        if role == "human":
            role = "user"
        elif role == "ai":
            role = "assistant"
        else:
            continue
        content = m.content if isinstance(m.content, str) else ""
        if content:
            out.append({"role": role, "content": content})
    return out


async def _fetch_transcript(request: Request, thread_id: str) -> list[dict[str, str]]:
    """读 checkpointer 原文。存储不可用 → 503(显式数据查询 fail-closed,
    不给「空会话」假象——同 ADR-0005 写路径哲学)。"""
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="记忆存储不可用,稍后重试")
    raw = await _load_checkpoint_messages(checkpointer, thread_id)
    return _project_messages(raw)


async def _load_checkpoint_messages(checkpointer: Any, thread_id: str) -> list:
    """按 saver 能力取原文消息:高级 aget_state(values.messages)优先,
    低层 aget_tuple(checkpoint.channel_values.messages)兜底(联调版本两者只居一)。"""
    config = {"configurable": {"thread_id": thread_id}}
    if hasattr(checkpointer, "aget_state"):
        state = await checkpointer.aget_state(config)
        values = getattr(state, "values", None) or {}
        return values.get("messages") or []
    tp = await checkpointer.aget_tuple(config)
    checkpoint = getattr(tp, "checkpoint", None) or {}
    channel_values = checkpoint.get("channel_values") or {}
    return channel_values.get("messages") or []


@router.get("/sessions")
async def list_my_sessions(
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> dict[str, Any]:
    """我的历史会话:agent_threads 按属主列出 + conversation_log 活跃度聚合。"""
    from official_agent.state.conversation import session_overview
    from official_agent.state.threads import list_active_threads

    identity, _ = auth
    user_id = identity.get("user_id")
    threads = (
        await asyncio.to_thread(list_active_threads, user_id) if user_id is not None else []
    )
    overview = await asyncio.to_thread(session_overview, [t.thread_id for t in threads])
    items = [
        {
            "thread_id": t.thread_id,
            "channel": t.channel,
            "subject": t.subject,
            "created_at": t.created_at,
            **overview.get(t.thread_id, {"rounds": 0, "last_at": None, "preview": ""}),
        }
        for t in threads
    ]
    # ISO 时间字符串字典序 = 时间序;str() 兜底混型(测试替身 None/str)
    items.sort(
        key=lambda x: str(x["last_at"] or x["created_at"] or ""),
        reverse=True,
    )
    return {"items": items}


@router.get("/sessions/{thread_id}/messages")
async def get_my_session_messages(
    request: Request,
    thread_id: str,
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> dict[str, Any]:
    """回看自己的会话原文。resolve_thread 硬校验属主——
    非属主/已终结/不存在一律 404(不区分,防会话枚举翻看 PII)。"""
    from official_agent.state.threads import resolve_thread

    identity, _ = auth
    user_id = identity.get("user_id")
    if user_id is None or await asyncio.to_thread(resolve_thread, thread_id, user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = await _fetch_transcript(request, thread_id)
    return {"thread_id": thread_id, "messages": messages}


@router.delete("/sessions/{thread_id}")
async def delete_my_session(
    request: Request,
    thread_id: str,
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> Response:
    """用户删除自己的会话:档案行物理 DELETE + checkpoint
    三表物理清理 + 对话日志物理清理;进程内运行时对象同步移除。

    Langfuse trace 的删除需经其外部 API(自托管/云与 trace 关联键尚未确定)
    ——本端点删除后 trace 已无可关联档案,残余 trace 按留存策略
    过期,运维手册见 docs/eval-observability.md 同级说明。"""
    from official_agent.state.conversation import delete_thread_conversations
    from official_agent.state.pg import purge_thread_checkpoints
    from official_agent.state.threads import hard_delete_thread, resolve_thread

    identity, _ = auth
    user_id = identity.get("user_id")
    if user_id is None or await asyncio.to_thread(resolve_thread, thread_id, user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 整段删除持"session_store.deleting"登记——check 与清理之间不再有窗口。
    # 只在持锁时登记,顺带确认没有在途轮次(在途 generator 会复活已清数据)。
    # 登记后即放锁:清理走线程池,不能让 session_store.lock 被磁盘 IO 长占
    # (其余会话读走同一把锁)。
    async with session_store.lock:
        inflight = session_store.sessions.get(thread_id)
        if inflight is not None and inflight.turn_lock.locked():
            raise HTTPException(status_code=409, detail="会话正在回复中,请稍后再删除")
        if thread_id in session_store.deleting:
            raise HTTPException(status_code=409, detail="会话正在删除中")
        session_store.deleting.add(thread_id)
    try:
        # 顺序:先清数据面(checkpoint/对话日志),档案行最后——部分失败时
        # 会话仍可重试删除,不产生"档案已删、数据面成孤儿"的死状态。
        try:
            await asyncio.to_thread(purge_thread_checkpoints, thread_id)
            await asyncio.to_thread(delete_thread_conversations, thread_id)
        except Exception as exc:  # noqa: BLE001 — 删除不完整必须如实暴露
            raise HTTPException(
                status_code=500, detail="会话数据清理失败,请稍后重试或联系管理员"
            ) from exc
        deleted = await asyncio.to_thread(
            hard_delete_thread, thread_id, owner_user_id=int(user_id)
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="会话不存在")
        async with session_store.lock:
            session_store.sessions.pop(thread_id, None)
            session_store.last_access.pop(thread_id, None)
    finally:
        # 无论成败都撤登记:失败路径要让用户能重试删除
        async with session_store.lock:
            session_store.deleting.discard(thread_id)
    return Response(status_code=204)


@router.get("/admin/sessions")
async def get_admin_sessions(
    request: Request,
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """管理员按用户查看会话列表;user_id 缺省 = 全部用户的会话。"""
    from official_agent.state.conversation import session_overview
    from official_agent.state.threads import list_active_threads

    user_id_raw = request.query_params.get("user_id")
    try:
        user_id = int(user_id_raw) if user_id_raw else None
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id 必须为整数") from None
    threads = await asyncio.to_thread(list_active_threads, user_id)
    overview = await asyncio.to_thread(session_overview, [t.thread_id for t in threads])
    items = [
        {
            "thread_id": t.thread_id,
            "owner_user_id": t.owner_user_id,
            "channel": t.channel,
            "subject": t.subject,
            "created_at": t.created_at,
            **overview.get(t.thread_id, {"rounds": 0, "last_at": None, "preview": ""}),
        }
        for t in threads
    ]
    # ISO 时间字符串字典序 = 时间序;str() 兜底混型(测试替身 None/str)
    items.sort(
        key=lambda x: str(x["last_at"] or x["created_at"] or ""),
        reverse=True,
    )
    return {"items": items}


@router.get("/admin/sessions/{thread_id}/messages")
async def get_admin_session_messages(
    request: Request,
    thread_id: str,
    identity: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """管理员回看任意会话原文;不存在 404。已终结会话原文仍可查
    (status 标注返回),供运营排查——区别于用户侧 resolve_thread 拒绝复活。
    管理员原文读取落审计(actor/thread/时间),fail-open 不阻断读取。"""
    from official_agent.state.audit import write_audit
    from official_agent.state.threads import get_thread

    rec = await asyncio.to_thread(get_thread, thread_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    try:
        await asyncio.to_thread(
            write_audit,
            thread_id=thread_id,
            acting_user_id=int(identity.get("user_id") or 0),
            channel="web",
            agent="admin-console",
            action={
                "op": "admin_read_transcript",
                "thread_id": thread_id,
                "owner_user_id": rec.owner_user_id,
            },
            decision="admin:read_transcript",
            result=f"管理员回看会话原文(owner={rec.owner_user_id},status={rec.status})",
        )
    except Exception:  # noqa: BLE001 — 读取审计缺失必须可见但不阻断
        logger.warning("管理员原文读取审计写入失败(thread=%s)", thread_id, exc_info=True)
    messages = await _fetch_transcript(request, thread_id)
    return {
        "thread_id": thread_id,
        "owner_user_id": rec.owner_user_id,
        "status": rec.status,
        "messages": messages,
    }
