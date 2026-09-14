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
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from official_agent.graphs.assistant import (
    _ROLE_TOOL_NAMES,  # noqa: PLC2701 — 同模块装配表(prefix_hash 证据)
    build_assistant_agent,
    tool_roster,
)
from official_agent.graphs.assistant.compression import (
    maybe_compress,
    summarize_messages,
)
from official_agent.graphs.identity import ResolvedIdentity, resolve
from official_agent.observability import langfuse_callbacks
from official_agent.security.pii import ReplyPiiMasker
from official_agent.state.threads import create_thread, new_thread_id, resolve_thread
from official_agent.tools.client import BackendError, BackendUnavailableError
from official_agent.tools.readonly import asker_scope

router = APIRouter()

logger = logging.getLogger(__name__)

# 会话列表排序兜底:agent_threads.created_at 表级 NOT NULL,测试替身可能给 None
_SORT_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _SessionState:
    """一个会话的运行时状态:装配好的 agent + 身份 + 用户 JWT。

    checkpointer(记忆)是共享的(进程级,thread_id 隔离);这里只存每个会话
    不能共享的东西:绑定该用户 token 的 agent(见模块 docstring)。
    """

    def __init__(
        self,
        session_id: str,
        identity: ResolvedIdentity,
        user_token: str,
        agent: Any,
    ) -> None:
        self.session_id = session_id
        self.identity = identity
        self.user_token = user_token
        self.agent = agent
        # 配置热生效:agent 装配时的配置指纹(HOT_KEYS 值哈希)。
        # PUT /admin/config 后下一轮比对发现不同 → 重建 agent 用新配置。
        self.applied_config_fingerprint: str | None = None
        # 轮计数(1 起),压缩事件留痕「触发轮」用
        self.turns = 0
        # 同会话并发轮次串行化——_stream_turn 全程持有,
        # 第二个并发请求按 SSE 错误码契约立即回 busy(不入队;单 worker 下即全部防线)
        self.turn_lock = asyncio.Lock()


# 会话注册表:session_id → 运行时状态。进程内存,单 worker 语义(多副本部署下
# 不得依赖进程内表做权限判断——恢复走 PG 档案 resolve_thread)。
# 有界 TTL+LRU——超容/空闲过期只淘汰运行时对象;会话档案与 checkpoint
# 在 PG,淘汰后携原 session_id 重入会走 resolve_thread 恢复路径,上下文不丢。
_sessions: OrderedDict[str, _SessionState] = OrderedDict()
# #194 复审:正在删除的 session_id 集合——删除期间拒绝新轮次。
# 仅靠 turn_lock 不够:会话不在内存(重启/LRU 淘汰)时,续传路径会为同一
# thread_id 重建**新对象 + 新锁**,旧锁的检查对它无效。删除开始即在此登记,
# _get_or_create_session 两条分支(内存命中/档案恢复)都先查它。
_deleting_sessions: set[str] = set()
_sessions_last_access: dict[str, float] = {}
_sessions_lock = asyncio.Lock()

# 恢复拒绝的统一文案:不存在/跨属主/已终结不区分(防会话枚举翻看)
_SESSION_RESUME_REJECT = "会话不存在、已结束或无权访问"


def _evict_sessions_locked(now: float) -> None:
    """TTL+LRU 淘汰(调用方持 _sessions_lock)。只删运行时,不碰 PG。

    在途轮次(turn_lock 被持)一律不淘汰。
    淘汰执行中的 _SessionState 会让同一 session_id 的下一个请求在内存
    未命中,走 resolve_thread 恢复路径重建出一个**新对象 + 新锁**,两个
    agent 随后并发写同一 checkpoint thread(旧对象还在 astream 里)。
    容量不足且全部在途时宁可不淘汰,留待轮末下一次调用再清。
    """
    from official_agent.config import get_settings

    settings = get_settings()
    ttl = max(int(settings.session_registry_ttl_seconds), 1)
    cap = max(int(settings.session_registry_max), 1)

    def _evictable(sid: str) -> bool:
        state = _sessions.get(sid)
        return state is not None and not state.turn_lock.locked()

    expired = [
        sid for sid, ts in _sessions_last_access.items() if now - ts > ttl and _evictable(sid)
    ]
    for sid in expired:
        _sessions.pop(sid, None)
        _sessions_last_access.pop(sid, None)
    # LRU:从最旧开始挑可淘汰项,在途的跳过(不阻断其余淘汰)
    while len(_sessions) > cap:
        victim = next((sid for sid in _sessions if _evictable(sid)), None)
        if victim is None:  # 全部在途:本轮放弃淘汰,不制造双锁
            break
        _sessions.pop(victim, None)
        _sessions_last_access.pop(victim, None)


async def _authenticate(request: Request, authorization: Annotated[str | None, Header()] = None):
    """Authorization: Bearer <官网JWT> → (身份, 官网JWT)。

    官网 JWT 两用:① resolve 换身份(只查 /auth/me)② 原样绑定给工具
    查本人数据(get_as_user 裸发)。JWT 只存会话态,不进 checkpointer。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token 为空")

    try:
        identity = await resolve({"kind": "web", "token": token})  # type: ignore[typeddict-item]
    except BackendUnavailableError as exc:
        # 后端网络/服务故障是 503(可重试),不与凭证错误混为 401
        raise HTTPException(status_code=503, detail="认证服务暂时不可用,请稍后重试") from exc
    except BackendError as exc:  # 凭证无效/过期等
        raise HTTPException(status_code=401, detail="身份解析失败") from exc
    return identity, token


async def _get_or_create_session(
    request: Request,
    identity: ResolvedIdentity,
    user_token: str,
    session_id: str | None,
) -> tuple[_SessionState, bool]:
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
    async with _sessions_lock:
        # #194 复审:删除中的会话一律拒绝——先于内存命中/档案恢复两条分支。
        # 删除端在持锁登记后立刻放锁去做磁盘清理;此处若放行,新轮次会与
        # 清理并发读写同一 checkpoint thread(要么复活已删数据,要么读到半删状态)。
        if session_id and session_id in _deleting_sessions:
            raise HTTPException(status_code=409, detail="会话正在删除中")
        if session_id and session_id in _sessions:
            existing = _sessions[session_id]
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
            _sessions.move_to_end(session_id)
            _sessions_last_access[session_id] = now
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
            session = _SessionState(session_id, identity, user_token, agent)
            session.applied_config_fingerprint = _config_fingerprint()
            _sessions[session_id] = session
            _sessions_last_access[session_id] = now
            _evict_sessions_locked(now)
            return session, False

        # thread_id:建档优先;PG 不可用降级随机 thread_id(保隔离,不持久化)
        try:
            if user_id is not None:
                session_id = create_thread("web", user_id, subject="web-chat").thread_id
            else:
                session_id = new_thread_id("web", 0)
        except Exception:  # noqa: BLE001 — 建档失败不阻断对话(与 CLI 同语义)
            session_id = new_thread_id("web", user_id or 0)

        # checkpointer:进程级共享(app lifespan 建立,fail-open)。thread_id 隔离会话。
        checkpointer = getattr(request.app.state, "checkpointer", None)
        agent = build_assistant_agent(identity, user_token=user_token, checkpointer=checkpointer)
        session = _SessionState(session_id, identity, user_token, agent)
        # 新建即记录当前配置指纹,避免首轮 _ensure_fresh_agent_config 误重建
        session.applied_config_fingerprint = _config_fingerprint()
        _sessions[session_id] = session
        _sessions_last_access[session_id] = now
        _evict_sessions_locked(now)
        return session, True


@router.post("/chat")
async def chat(
    request: Request,
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> StreamingResponse:
    """一轮对话(SSE 流)。body: {"message": str, "session_id": str | null}

    首次(session_id 空)→ 服务端生成 session_id 并随流返回;续传带原 session_id。
    SSE 事件(data 为 JSON):session(带 created) / delta(role,content) /
    tool(role,name) / done / error(code,message)。
    """
    identity, user_token = auth
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")
    if len(message) > _MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail=f"消息过长(上限 {_MAX_MESSAGE_CHARS} 字)")
    session_id = (body.get("session_id") or "").strip() or None

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

# auth 失效的关键词(get_as_user 失败文案含之;message 判定的最后兜底)。
_AUTH_FAIL_HINTS = ("令牌", "token", "登录", "JWT")
# 单条消息长度上限(先收敛单请求滥用面;超限走 400 invalid_request,前端零改动)
_MAX_MESSAGE_CHARS = 2000

# 全局活跃模型调用并发闸(跨用户资源保护;进程内,数值待定)
_model_gate: asyncio.Semaphore | None = None


def _get_model_gate() -> asyncio.Semaphore:
    global _model_gate
    if _model_gate is None:
        from official_agent.config import get_effective_settings

        _model_gate = asyncio.Semaphore(
            max(int(get_effective_settings().model_call_global_concurrency), 1)
        )
    return _model_gate


def _sse_error(code: str) -> dict[str, str]:
    """稳定错误事件:安全文案 + trace_id,绝不带原始异常串。"""
    from official_agent.observability import current_trace_id

    copy = _ERR_SAFE_COPY.get(code) or _ERR_SAFE_COPY[_ERR_UNKNOWN]
    return {"type": "error", "code": code, "message": f"{copy}(trace:{current_trace_id()})"}


def _error_code(exc: Exception) -> str:
    """执行期异常 → 面向客户端的稳定错误码。分类原则:
    - 用户令牌失效(get_as_user 文案)或明确登录/token 问题 → auth_expired
    - BackendUnavailableError(网络/传输故障)→ backend_unavailable
    - httpx 传输/超时 → backend_unavailable(后端不可达/网关错)
    - 其余 BackendError(业务错误)按其文案;未知 → unknown
    观测/模型错误由 LangGraph 包装,不易精确识别,归 unknown(前端可重试)。
    """
    import httpx

    from official_agent.tools.client import BackendUnavailableError

    text = str(exc)
    if any(h in text for h in _AUTH_FAIL_HINTS):
        return _ERR_AUTH_EXPIRED
    if isinstance(exc, (httpx.HTTPError, BackendUnavailableError)):
        return _ERR_BACKEND_UNAVAILABLE
    if isinstance(exc, BackendError):
        # 业务错误(如「未投递」)不是系统故障——按 invalid_request 让前端展示 message
        return _ERR_INVALID_REQUEST
    return _ERR_UNKNOWN


def _config_fingerprint() -> str:
    """当前生效配置(HOT_KEYS 值)的指纹;配置变更即变化。"""
    from official_agent.config import HOT_KEYS, get_effective_settings

    settings = get_effective_settings()
    return repr(tuple((k, getattr(settings, k, None)) for k in sorted(HOT_KEYS)))


def _ensure_fresh_agent_config(session: _SessionState, checkpointer: Any) -> None:
    """配置热生效:比对配置指纹,变了则重建 session 的 agent。

    PUT /admin/config 只失效 get_settings 缓存;活跃会话的 agent 是进程内
    复用的(见模块 docstring),不重建就一直用旧 model/provider。此处每轮
    比对指纹,发现变化即用新配置重建 agent(身份/token 不变)。
    """
    try:
        current = _config_fingerprint()
    except Exception:  # noqa: BLE001 — PG 不可用 → 指纹取 env(不重建)
        return
    if session.applied_config_fingerprint == current:
        return
    session.agent = build_assistant_agent(
        session.identity, user_token=session.user_token, checkpointer=checkpointer
    )
    session.applied_config_fingerprint = current


async def _compress_if_needed(session: _SessionState, config: dict, user_query: str) -> str | None:
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
        settings = get_effective_settings()
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


async def _stream_turn(
    session: _SessionState, message: str, is_new: bool, checkpointer: Any = None
) -> AsyncIterator[str]:
    """跑一轮:流式吐 SSE。is_new 仅作 SSE session 事件的 created 标记。

    每轮结束在 conversation_log 落一行:
    - 正常:user_message(问题原文) + reply_summary(回复摘要非全文) + tools/耗时
    - 异常(error 事件):只存 error_code + 耗时,不存对话内容
    落行失败(fail-open)不阻断对话——观测绝不拖垮主流程(ADR-0005)。
    checkpointer:配置变更后重建 agent 需要(见 _ensure_fresh_agent_config)。
    """

    def sse(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 同会话并发轮次串行化——锁被占用时立即回 busy,
    # 不排队(前端提示「上一条还在回复中」);check/acquire 间无 await,原子。
    if session.turn_lock.locked():
        yield sse({"type": "error", "code": _ERR_BUSY, "message": "上一条消息还在回复中,请稍候"})
        return
    await session.turn_lock.acquire()
    try:
        # 配置热生效:配置指纹变了 → 重建 agent(新 model/provider 立即作用于本轮)
        _ensure_fresh_agent_config(session, checkpointer)
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
        yield sse({"type": "session", "session_id": session.session_id, "created": is_new})

        started = time.monotonic()
        tools_called: list[str] = []
        reply_chunks: list[str] = []
        # 无工具档缓冲整段回复,流尾过编造守卫后一次性下发
        toolless = not tool_roster(session.identity)
        # 流式 delta 逐块过 PII 掩码器(尾部缓冲抗跨块)
        pii_masker = None if toolless else ReplyPiiMasker()
        error_code: str | None = None
        usage_acc: dict[str, int | None] = {  # 跨 model 步累计(ReAct 多步求和)
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
        }
        _last_usage: dict[str, int | None] | None = None
        # 只读查询以来问者本人 JWT 执行(ADR-0006「社团官网层问答助手」):
        # 本轮内 readonly 查询经 _read 走 get_as_user,后端按本人权限判+归因
        async with asker_scope(session.user_token):
            # 全局活跃模型调用闸(跨用户资源保护)+ 单轮墙钟超时——
            # 卡死的模型/工具调用在配置时限内被取消,turn_lock 随 with 释放。
            from official_agent.config import get_effective_settings

            _settings = get_effective_settings()
            config["recursion_limit"] = max(int(_settings.turn_recursion_limit), 1)
            gate = _get_model_gate()
            try:
                gate_acquired = False
                try:
                    await asyncio.wait_for(
                        gate.acquire(),
                        timeout=max(int(_settings.model_gate_acquire_timeout), 1),
                    )
                    gate_acquired = True
                except TimeoutError:
                    # 闸满不提前 return——闸满也是「一轮失败」，
                    # 必须走下方统一落账路径，否则资源饱和事件从
                    # conversation_log 消失(收尾点唯一:正常/错误/超时/
                    # gate-busy/断连五条路径都落一行)。
                    error_code = _ERR_BUSY
                    logger.warning(
                        "chat turn gate busy session=%s turns=%d concurrency=%d",
                        session.session_id,
                        session.turns,
                        _settings.model_call_global_concurrency,
                    )
                else:
                    async with asyncio.timeout(max(int(_settings.turn_wall_clock_timeout), 1)):
                        async for mode, payload in session.agent.astream(  # type: ignore[attr-defined]
                            {"messages": messages},
                            config=config,
                            stream_mode=["messages", "updates"],
                        ):
                            if mode == "messages":
                                chunk, _meta = payload
                                if isinstance(chunk, AIMessageChunk) and chunk.content:
                                    # 只收文本块;多模态 content(list)跳过文本拼接(回复摘要仅文本)
                                    text = chunk.content if isinstance(chunk.content, str) else ""
                                    if text:
                                        reply_chunks.append(text)
                                        if pii_masker:
                                            out = pii_masker.feed(text)
                                            if out:
                                                yield sse(
                                                    {
                                                        "type": "delta",
                                                        "role": "assistant",
                                                        "content": out,
                                                    }
                                                )
                                # usage:只在 usage 终块累计,同值去重防重复计数。
                                # 两种形状二选一(langchain-openai 1.x 流式 raw
                                # token_usage 已消失,只剩 usage_metadata;DeepSeek 原始形状
                                # 保留兼容)。raw 优先(prompt_cache 字段只在原始)。
                                um = getattr(chunk, "usage_metadata", None)
                                raw_usage = (chunk.response_metadata or {}).get("token_usage")
                                usage_payload = raw_usage if raw_usage else um
                                if um is not None and usage_payload:
                                    extracted = extract_usage(usage_payload)
                                    if extracted != _last_usage:  # 同值跳过(跨 chunk 累计值重复)
                                        _last_usage = extracted
                                        for k in usage_acc:
                                            v = extracted.get(k)
                                            cur = usage_acc.get(k) or 0
                                            if v is not None:
                                                usage_acc[k] = cur + v
                            elif mode == "updates":
                                for _ns, node_update in payload.items():
                                    if isinstance(node_update, dict):
                                        for m in node_update.get("messages") or []:
                                            # 工具调用状态(tool 事件,role=tool)
                                            if getattr(m, "tool_calls", None):
                                                for tc in m.tool_calls:
                                                    tools_called.append(tc.get("name") or "")
                                                    yield sse(
                                                        {
                                                            "type": "tool",
                                                            "role": "tool",
                                                            "name": tc.get("name"),
                                                        }
                                                    )
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
                    usage_acc,
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

        # 单一写入路径:正常(error_code None)/错误/断连三态合一,落一行。
        # 缓存命中证据:缓存前缀稳定性 hash(system prompt + 角色工具名)。
        # 同 role 的会话前缀应逐字节稳定;hash 变化 = 前缀失效(命中率不可信)。
        # 实际 system = 静态正文 + 身份段 + 工具契约(随角色变);
        # hash 必须基于真实 system,否则"缓存命中证据"失真。同角色会话内
        # 前缀稳定(身份块同 session 不变),仍可作命中率观察。
        from official_agent.graphs.assistant import build_system_prompt

        # 编造守卫在一切持久化之前——直播(缓冲 delta)、
        # conversation_log、checkpointer(回看/下轮上下文)三面同用改写文本。
        # 压缩(_compress_if_needed 会 update_state 重写历史)必须排在守卫
        # 回写之后,否则会把编造原文一并压进摘要。
        if toolless and error_code is None:
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
                reply_chunks = [final_reply]
                await _rewrite_last_ai_message(session.agent, config, final_reply)
            if final_reply:
                yield sse({"type": "delta", "role": "assistant", "content": final_reply})

        # 回复出口 PII 守卫——全部会话适用。流式
        # delta 经 ReplyPiiMasker 逐块掩(尾部缓冲抗跨块);工具侧 deep 掩为
        # 主,此处为输出面兜底;命中即 trace(guard_event)。
        if pii_masker:
            tail = pii_masker.finish()
            if tail:
                yield sse({"type": "delta", "role": "assistant", "content": tail})

        # 轮末按需压缩(先压缩后落行,同一行携带 compress_event)。
        # 在 done 事件前执行:失败 fail-open 返回 None,不阻断 done。
        # 失败/超时/断连轮次不压缩——失败轮的残缺上下文不值得进摘要。
        compress_event = None
        if error_code is None:
            compress_event = await _compress_if_needed(session, config, message)
        role = session.identity.get("role") or "unknown"
        tool_names = list(_ROLE_TOOL_NAMES.get(role, ()))
        p_hash = prefix_hash(build_system_prompt(session.identity), tool_names)
        if any(usage_acc.values()):
            usage = usage_acc
        else:
            usage = {
                "input_tokens": None,
                "output_tokens": None,
                "cache_hit_tokens": None,
                "cache_miss_tokens": None,
            }
        _log_conversation(
            session,
            user_message=message,
            reply_summary="".join(reply_chunks),
            tools=tools_called,
            duration_ms=_elapsed_ms(started),
            error_code=error_code,
            usage=usage,
            prefix_hash=p_hash,
            compress_event=compress_event,
        )
        if error_code is None:
            yield sse({"type": "done", "session_id": session.session_id})
        elif error_code != _ERR_DISCONNECTED:
            # 错误事件统一走稳定文案 + trace_id(原始异常只留服务端日志);
            # 断连无需事件(客户端已不在)。
            yield sse(_sse_error(error_code))
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


def _log_conversation(
    session: _SessionState,
    *,
    user_message: str,
    reply_summary: str,
    tools: list[str],
    duration_ms: int,
    error_code: str | None = None,
    usage: dict[str, int | None] | None = None,
    prefix_hash: str | None = None,
    compress_event: str | None = None,
) -> None:
    """落一行 conversation_log(fail-open,非阻塞)。

    lazy import:web 入口不顶层依赖 psycopg(无 PG 环境可跑服务,
    观测侧不给主链路加硬依赖,同 app.py lifespan 先例)。
    fire-and-forget:写入放后台任务,不阻塞 SSE 流尾(ADR-0005 fail-open)。
    """
    from official_agent.state.conversation import write_conversation

    async def _write() -> None:
        try:
            write_conversation(
                thread_id=session.session_id,
                user_id=session.identity.get("user_id"),
                channel=session.identity.get("source") or "web",
                user_message=user_message,
                reply_summary=reply_summary,
                tools=tools,
                duration_ms=duration_ms,
                error_code=error_code,
                prefix_hash=prefix_hash,
                compress_event=compress_event,
                **(usage or {}),
            )
        except Exception:  # noqa: BLE001 — 观测写入失败不拖垮对话(ADR-0005)
            logger.warning("conversation_log 写入失败(已忽略)", exc_info=True)

    asyncio.create_task(_write())


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def extract_usage(usage_metadata: dict[str, Any] | None) -> dict[str, int | None]:
    """从 LLM usage_metadata 提取 token 数(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.conversation import extract_usage as _impl

    return _impl(usage_metadata)


def prefix_hash(system_prompt: str, tool_names: list[str]) -> str:
    """缓存前缀稳定性 hash(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.conversation import prefix_hash as _impl

    return _impl(system_prompt, tool_names)


# ── 管理 API:配置热生效 ────────────────────────────────────────

# 高敏键(Settings 字段名):真实凭证,只读回显掩码,永不入库/不可在线改。
_SECRET_SETTINGS_FIELDS: frozenset[str] = frozenset(
    {
        "backend_service_username",
        "backend_service_password",
        "llm_api_key",
        "anthropic_api_key",
        "postgres_url",
        "feishu_app_id",
        "feishu_app_secret",
        "feishu_verification_token",
        "feishu_encrypt_key",
        "langfuse_public_key",
        "langfuse_secret_key",
    }
)


def _mask_secret(value: str) -> str:
    """掩码末 4 位(短值整掩)。"""
    return value[-4:] if len(value) >= 4 else "****"


async def _require_monitor(request: Request, authorization: Annotated[str | None, Header()] = None):
    """管理 API 认证:官网 JWT → resolve → permission_codes 含 agent:monitor。"""
    identity, _ = await _authenticate(request, authorization)
    codes = identity.get("permission_codes") or []
    if "agent:monitor" not in codes:
        raise HTTPException(status_code=403, detail="需要 agent:monitor 权限")
    return identity


@router.get("/admin/config")
async def get_admin_config(
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """回显配置:低敏键实值(DB 覆盖优先) + 高敏键掩码({configured, masked})。"""
    from official_agent.config import HOT_KEYS, get_settings

    settings = get_settings()
    try:
        db_overrides = get_all_config()
    except Exception:  # noqa: BLE001 — PG 不可用 → 只显示 env(fail-open)
        db_overrides = {}

    result: dict[str, Any] = {}
    for field in HOT_KEYS:
        result[field] = db_overrides.get(field, getattr(settings, field, ""))
    for field in sorted(_SECRET_SETTINGS_FIELDS):
        value = getattr(settings, field, "") or ""
        result[field] = {
            "configured": bool(value),
            "masked": _mask_secret(value) if value else "",
        }
    return result


# 安全:llm_base_url 若被改成任意端点,下轮重建 agent 时
# .env 的 LLM key 会作为 Bearer 发往该端点 → key 泄漏。只允许 https + 受信 host。
_ALLOWED_LLM_HOSTS: tuple[str, ...] = (
    "api.deepseek.com",
    "api.openai.com",
    "api.anthropic.com",
    "open.bigmodel.cn",
)


def _validate_base_url(value: str) -> None:
    import urllib.parse

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="llm_base_url 必须为 https 且含 host")
    if parsed.hostname not in _ALLOWED_LLM_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=f"llm_base_url 的 host 不在白名单: {parsed.hostname}",
        )


@router.put("/admin/config")
async def put_admin_config(
    body: dict[str, str],
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """改低敏键(HOT_KEYS 白名单)并热生效;非白名单(高敏)→ 400。"""
    from official_agent.config import HOT_KEYS

    invalid_keys = [k for k in body if k not in HOT_KEYS]
    if invalid_keys:
        raise HTTPException(status_code=400, detail=f"不可热载的键: {', '.join(invalid_keys)}")
    for key, value in body.items():
        value = (value or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"{key} 值不能为空")
        if key == "llm_base_url":
            _validate_base_url(value)
        set_config(key, value)
    # 全部键成功写库后才失效缓存(避免部分写 + 缓存未刷的分离)
    invalidate_settings_cache()
    return {"updated": list(body.keys())}


def get_all_config() -> dict[str, str]:
    """读 agent_config 全部键值(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.config_store import get_all_config as _impl

    return _impl()


def set_config(key: str, value: str) -> None:
    """upsert 一个配置键(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.config_store import set_config as _impl

    _impl(key, value)


def invalidate_settings_cache() -> None:
    """使 get_settings 缓存失效(lazy;模块级包装供测试 patch)。"""
    from official_agent.config import invalidate_settings_cache as _impl

    _impl()


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
    items = list_conversations(user_id=user_id, thread_id=thread_id, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/admin/conversations/{conversation_id}")
async def get_admin_conversation_detail(
    conversation_id: int,
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """对话详情:轮次/工具/耗时/错误码 + 可展开回复摘要。"""
    row = get_conversation(conversation_id)
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
    threads = list_active_threads(user_id) if user_id is not None else []
    overview = session_overview([t.thread_id for t in threads])
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
    if user_id is None or resolve_thread(thread_id, user_id) is None:
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
    if user_id is None or resolve_thread(thread_id, user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 整段删除持"_deleting_sessions"登记——check 与清理之间不再有窗口。
    # 只在持锁时登记,顺带确认没有在途轮次(在途 generator 会复活已清数据)。
    # 登记后即放锁:清理走线程池,不能让 _sessions_lock 被磁盘 IO 长占
    # (其余会话读走同一把锁)。
    async with _sessions_lock:
        inflight = _sessions.get(thread_id)
        if inflight is not None and inflight.turn_lock.locked():
            raise HTTPException(status_code=409, detail="会话正在回复中,请稍后再删除")
        if thread_id in _deleting_sessions:
            raise HTTPException(status_code=409, detail="会话正在删除中")
        _deleting_sessions.add(thread_id)
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
        async with _sessions_lock:
            _sessions.pop(thread_id, None)
            _sessions_last_access.pop(thread_id, None)
    finally:
        # 无论成败都撤登记:失败路径要让用户能重试删除
        async with _sessions_lock:
            _deleting_sessions.discard(thread_id)
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
    threads = list_active_threads(user_id)
    overview = session_overview([t.thread_id for t in threads])
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

    rec = get_thread(thread_id)
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
