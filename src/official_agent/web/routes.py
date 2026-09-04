"""客服 Agent SSE 聊天路由(INF-04)。

会话模型(与调研/决议一致):
- 会话 = thread_id(SEC-07 格式 web:u{user}:{rand8}),首次 POST 建档并返回给前端;
  续传带原 session_id(= thread_id)。会话记忆在共享 checkpointer(thread_id 维度),
  不在进程对象 —— 进程重启后同 session_id 可重建 agent 并续上下文。
- agent 按「身份 × user_token」装配(assemble_tools 把官网 JWT 闭包绑定到
  get_my_interview,#93/#89 决策:数据查询 JWT 直转),故每会话持有一个 agent;
  待工具改为从图 state 取 token(M3)后可共享单 graph。
- 权限:身份经 resolve(kind=web,#89 A2:调后端 /auth/me)。后端未落地前 resolve
  抛 NotImplementedError → 本路由 501,绝不放开匿名/模拟身份进生产路径。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage

from official_agent.graphs.assistant import build_assistant_agent, identity_message
from official_agent.graphs.identity import ResolvedIdentity, resolve
from official_agent.observability import langfuse_callbacks
from official_agent.state.threads import create_thread, new_thread_id

router = APIRouter()


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


# 会话注册表:session_id → 运行时状态。进程内存,单 worker 语义(多副本 INF-11)。
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
    except NotImplementedError as exc:
        # #89 后端 /auth/me 未落地:真实身份通道未接通,显式拒绝(501),
        # 不放开匿名/模拟身份进生产路径。
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:  # BackendError/httpx:凭证错/后端不可达
        raise HTTPException(status_code=401, detail=f"身份解析失败: {exc}") from exc
    return identity, token


async def _get_or_create_session(
    request: Request,
    identity: ResolvedIdentity,
    user_token: str,
    session_id: str | None,
) -> _SessionState:
    """取会话;无则(或未给)新建并建档。同一 session_id 只能被同 user 续传(SEC-07)。"""
    async with _sessions_lock:
        if session_id and session_id in _sessions:
            existing = _sessions[session_id]
            if existing.identity.get("user_id") != identity.get("user_id"):
                raise HTTPException(status_code=403, detail="无权访问该会话")
            return existing

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
        _sessions[session_id] = session
        return session


@router.post("/chat")
async def chat(
    request: Request,
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> StreamingResponse:
    """一轮对话(SSE 流)。body: {"message": str, "session_id": str | null}

    首次(session_id 空)→ 服务端生成 session_id 并随流返回;续传带原 session_id。
    SSE 事件(data 为 JSON):{"type":"session_id","session_id":…} /
    {"type":"token","content":…} / {"type":"done"}。工具状态以 token 流前置标注。
    """
    identity, user_token = auth
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")
    session_id = (body.get("session_id") or "").strip() or None

    session = await _get_or_create_session(request, identity, user_token, session_id)
    return StreamingResponse(
        _stream_turn(session, message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_turn(session: _SessionState, message: str) -> AsyncIterator[str]:
    """跑一轮:流式吐 SSE。历史/首轮前缀判据复用 CLI 模式(cli.py:211-221)。"""
    config = {
        "configurable": {"thread_id": session.session_id},
        "callbacks": langfuse_callbacks(),
    }

    def sse(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield sse({"type": "session_id", "session_id": session.session_id})

    # 首轮注入身份前缀:checkpointer 无该 thread 历史 = 新会话(cli.py 持久化判据)。
    first_input = HumanMessage(content=identity_message(session.identity))
    state = None
    try:
        state = await session.agent.aget_state(config)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — 无 checkpointer(降级)按新会话处理
        state = None
    is_new = state is None
    messages: list = (
        [first_input, HumanMessage(content=message)] if is_new else [HumanMessage(content=message)]
    )

    try:
        async for mode, payload in session.agent.astream(  # type: ignore[attr-defined]
            {"messages": messages}, config=config, stream_mode=["messages", "updates"]
        ):
            if mode == "messages":
                chunk, _meta = payload
                if isinstance(chunk, AIMessageChunk) and chunk.content:
                    yield sse({"type": "token", "content": chunk.content})
            elif mode == "updates":
                for _ns, node_update in payload.items():
                    if isinstance(node_update, dict):
                        for m in node_update.get("messages") or []:
                            # 工具调用状态(CLI 以「→ 调用 X…」显示,SSE 发 tool_call 事件)
                            if getattr(m, "tool_calls", None):
                                for tc in m.tool_calls:
                                    yield sse({"type": "tool_call", "name": tc.get("name")})
    except Exception as exc:  # noqa: BLE001 — 单轮失败不崩连接,吐 error 事件
        yield sse({"type": "error", "message": str(exc)})
        return
    yield sse({"type": "done"})
