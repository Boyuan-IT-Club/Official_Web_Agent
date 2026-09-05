"""客服 Agent SSE 聊天路由(INF-04)。

会话模型(与调研/决议一致):
- 会话 = thread_id(SEC-07 格式 web:u{user}:{rand8}),首次 POST 建档并返回给前端;
  续传带原 session_id(= thread_id)。会话记忆在共享 checkpointer(thread_id 维度),
  不在进程对象 —— 进程重启后同 session_id 可重建 agent 并续上下文。
- agent 按「身份 × user_token」装配(assemble_tools 把官网 JWT 闭包绑定到
  get_my_interview,#93/#89 决策:数据查询 JWT 直转),故每会话持有一个 agent;
  待工具改为从图 state 取 token(M3)后可共享单 graph。
- 权限:身份经 resolve(kind=web,#89 A2:调后端 /auth/me,官网通道唯一入口)。
  身份解析失败 → 401;不放开匿名/模拟身份进生产路径。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage

from official_agent.graphs.assistant import build_assistant_agent, identity_message
from official_agent.graphs.identity import ResolvedIdentity, resolve
from official_agent.observability import langfuse_callbacks
from official_agent.state.threads import create_thread, new_thread_id
from official_agent.tools.client import BackendError

router = APIRouter()

logger = logging.getLogger(__name__)


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
        # M6 #111 热生效:agent 装配时的配置指纹(HOT_KEYS 值哈希)。
        # PUT /admin/config 后下一轮比对发现不同 → 重建 agent 用新配置。
        self.applied_config_fingerprint: str | None = None


# 会话注册表:session_id → 运行时状态。进程内存,单 worker 语义(多副本 INF-11)。
# NOTE: 无上限无过期——淘汰/限流留给 M3 会话层(Ably 调研):断线跨端/取消恢复
# 需要会话层,届时换持久会话而非进程内存表。
_sessions: dict[str, _SessionState] = {}
_sessions_lock = asyncio.Lock()


async def _authenticate(request: Request, authorization: Annotated[str | None, Header()] = None):
    """Authorization: Bearer <官网JWT> → (身份, 官网JWT)。

    官网 JWT 两用:① resolve 换身份(只查 /auth/me)② 原样绑定给工具
    查本人数据(get_as_user 裸发,#89 决策 4)。JWT 只存会话态,不进 checkpointer。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token 为空")

    try:
        identity = await resolve({"kind": "web", "token": token})  # type: ignore[typeddict-item]
    except Exception as exc:  # BackendError/httpx:凭证错/后端不可达
        raise HTTPException(status_code=401, detail="身份解析失败") from exc
    return identity, token


async def _get_or_create_session(
    request: Request,
    identity: ResolvedIdentity,
    user_token: str,
    session_id: str | None,
) -> tuple[_SessionState, bool]:
    """取会话;无则(或未给)新建并建档。同一 session_id 只能被同 user 续传(SEC-07)。

    返回 (session, is_new):is_new=True 表示本会话是进程内新建(首轮须注身份前缀),
    False 表示续传既有会话。进程重启后带旧 session_id 续传会命中新建分支 → is_new
    误判 True,但身份注入幂等(同身份重注无害),可接受;不依赖 aget_state
    (LangGraph 对无 checkpoint 的 thread 可能返回非 None,导致 is_new 恒 False,
    身份永不在首轮注入——实测坑)。
    """
    async with _sessions_lock:
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
            return existing, False

        user_id = identity.get("user_id")
        # thread_id(SEC-07):建档优先;PG 不可用降级随机 thread_id(保隔离,不持久化)
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
        return session, True


@router.post("/chat")
async def chat(
    request: Request,
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> StreamingResponse:
    """一轮对话(SSE 流)。body: {"message": str, "session_id": str | null}

    首次(session_id 空)→ 服务端生成 session_id 并随流返回;续传带原 session_id。
    SSE 事件(契约 #90,data 为 JSON):session(带 created) / delta(role,content) /
    tool(role,name) / done / error(code,message)。
    """
    identity, user_token = auth
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")
    session_id = (body.get("session_id") or "").strip() or None

    session, is_new = await _get_or_create_session(request, identity, user_token, session_id)
    checkpointer = getattr(request.app.state, "checkpointer", None)
    return StreamingResponse(
        _stream_turn(session, message, is_new, checkpointer),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# SSE 流内 error 事件 code(契约 #90)。前端按 code 决定动作(见 issue #90 冻结评论)。
_ERR_AUTH_EXPIRED = "auth_expired"
_ERR_BACKEND_UNAVAILABLE = "backend_unavailable"
_ERR_MODEL = "model_error"
_ERR_INVALID_REQUEST = "invalid_request"
_ERR_UNKNOWN = "unknown"
# 客户端断连中止(CancelledError):非 #90 契约码,运营侧新码——断连轮次留痕专用
_ERR_DISCONNECTED = "client_disconnected"
# auth 失效的关键词(get_as_user 失败文案含之;message 判定的最后兜底)。
_AUTH_FAIL_HINTS = ("令牌", "token", "登录", "JWT")


def _error_code(exc: Exception) -> str:
    """执行期异常 → 契约错误码。分类原则:
    - 用户令牌失效(get_as_user 文案)或明确登录/token 问题 → auth_expired(#94 核心)
    - httpx 传输/超时 → backend_unavailable(后端不可达/网关错)
    - 其余 BackendError(业务错误)按其文案;未知 → unknown
    观测/模型错误由 LangGraph 包装,不易精确识别,归 unknown(前端可重试)。
    """
    import httpx

    text = str(exc)
    if any(h in text for h in _AUTH_FAIL_HINTS):
        return _ERR_AUTH_EXPIRED
    if isinstance(exc, httpx.HTTPError):
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
    """M6 #111 热生效:比对配置指纹,变了则重建 session 的 agent。

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

async def _stream_turn(
    session: _SessionState, message: str, is_new: bool, checkpointer: Any = None
) -> AsyncIterator[str]:
    """跑一轮:流式吐 SSE。is_new 由会话层判定(新建才注身份前缀)。

    每轮结束在 conversation_log 落一行(M6 #110):
    - 正常:user_message(问题原文) + reply_summary(回复摘要非全文) + tools/耗时
    - 异常(error 事件):只存 error_code + 耗时,不存对话内容(决策 #102/#110)
    落行失败(fail-open)不阻断对话——观测绝不拖垮主流程(ADR-0005)。
    checkpointer:配置变更后重建 agent 需要(见 _ensure_fresh_agent_config)。
    """
    # M6 #111 热生效:配置指纹变了 → 重建 agent(新 model/provider 立即作用于本轮)
    _ensure_fresh_agent_config(session, checkpointer)

    config = {
        "configurable": {"thread_id": session.session_id},
        "callbacks": langfuse_callbacks(),
    }

    def sse(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 首轮注入身份前缀(仅进程内新建会话;续传不重复注)
    first_input = HumanMessage(content=identity_message(session.identity))
    messages: list = (
        [first_input, HumanMessage(content=message)] if is_new else [HumanMessage(content=message)]
    )

    # 契约 #90:首事件 session(带 created 标记新/续传)
    yield sse({"type": "session", "session_id": session.session_id, "created": is_new})

    started = time.monotonic()
    tools_called: list[str] = []
    reply_chunks: list[str] = []
    error_code: str | None = None
    usage_acc: dict[str, int | None] = {  # 跨 model 步累计(#113 MAJOR:ReAct 多步求和)
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
    }
    _last_usage: dict[str, int | None] | None = None
    try:
        async for mode, payload in session.agent.astream(  # type: ignore[attr-defined]
            {"messages": messages}, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, _meta = payload
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    # 只收文本块;多模态 content(list)跳过文本拼接(回复摘要仅文本)
                    text = chunk.content if isinstance(chunk.content, str) else ""
                    if text:
                        reply_chunks.append(text)
                        yield sse({"type": "delta", "role": "assistant", "content": text})
                # M6 #113 usage:原始响应 usage(response_metadata.token_usage 保留
                # DeepSeek 顶层 cache 字段;usage_metadata 会被 langchain 转换丢弃)。
                # 只在 usage 终块(usage_metadata 非 None)累计,且同值去重防重复计数
                # (传统流式 provider 在最终 usage-only chunk 才带 usage)。
                um = getattr(chunk, "usage_metadata", None)
                raw_usage = (chunk.response_metadata or {}).get("token_usage")
                if um is not None and raw_usage:
                    extracted = extract_usage(raw_usage)
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
                            # 工具调用状态(契约 #90:tool 事件,role=tool)
                            if getattr(m, "tool_calls", None):
                                for tc in m.tool_calls:
                                    tools_called.append(tc.get("name") or "")
                                    yield sse(
                                        {"type": "tool", "role": "tool", "name": tc.get("name")}
                                    )
    except Exception as exc:  # noqa: BLE001 — 单轮失败不崩连接,吐 error 事件
        error_code = _error_code(exc)
        yield sse({"type": "error", "code": error_code, "message": str(exc)})
    except asyncio.CancelledError:
        # 客户端断连(CancelledError 非 Exception):中止轮次也要留痕
        # (partial reply/已调工具不丢失),error_code 记断连中止。
        error_code = _ERR_DISCONNECTED

    # 单一写入路径:正常(error_code None)/错误/断连三态合一,落一行。
    # M6 #113 命中证据:缓存前缀稳定性 hash(system prompt + 角色工具名)。
    # 同 role 的会话前缀应逐字节稳定;hash 变化 = 前缀失效(命中率不可信)。
    from official_agent.graphs.assistant import _ROLE_TOOL_NAMES, load_system_prompt

    role = session.identity.get("role") or "unknown"
    tool_names = list(_ROLE_TOOL_NAMES.get(role, ()))
    p_hash = prefix_hash(load_system_prompt(), tool_names)
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
    )
    if error_code is None:
        yield sse({"type": "done", "session_id": session.session_id})


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

# ── M6 #111 管理 API:配置热生效 ────────────────────────────────────────

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


# 安全(评审 MINOR-1):llm_base_url 若被改成任意端点,下轮重建 agent 时
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
    # 全部键成功写库后才失效缓存(避免部分写 + 缓存未刷的分离,评审 MINOR-2)
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


# ── M6 #112 管理 API:运营视图(对话列表/详情) ───────────────────────────

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
    """运营列表:时间/用户/问题首字/状态,按 user_id 过滤 + 分页(#112)。"""
    user_id_raw = request.query_params.get("user_id")
    limit_raw = request.query_params.get("limit", "50")
    offset_raw = request.query_params.get("offset", "0")
    try:
        user_id = int(user_id_raw) if user_id_raw is not None else None
        limit = max(1, min(int(limit_raw), 200))
        offset = max(0, int(offset_raw))
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id/limit/offset 必须为整数") from None
    items = list_conversations(user_id=user_id, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/admin/conversations/{conversation_id}")
async def get_admin_conversation_detail(
    conversation_id: int,
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """对话详情:轮次/工具/耗时/错误码 + 可展开回复摘要(#112)。"""
    row = get_conversation(conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row
