"""INF-04 SSE 聊天路由测试:鉴权/会话隔离。

用 FastAPI TestClient,验证:
- 无 Authorization → 401
- 坏 token(/auth/me 拒)→ 401
- 后端 /auth/me 未落地(NotImplementedError)→ 501(显式护栏)
- 合法 token → 200,SSE 流返回 session_id + done
- 同 session_id 被不同 user 访问 → 403(SEC-07 属主)

路由层不真连后端/模型/PG:
- routes.resolve 被 monkeypatch(fake_resolve 返回 ResolvedIdentity)
- build_assistant_agent 被 monkeypatch(_FakeAgent 吐一条消息)
- lifespan 的 get_checkpointer 被 monkeypatch 成假 saver(None)
身份解析本身(_resolve_web→/auth/me)已在 test_identity.py 覆盖。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from official_agent.web.app import create_app


@contextlib.asynccontextmanager
async def _fake_checkpointer() -> AsyncIterator[None]:
    yield None


class _FakeAgent:
    """最小假 agent:astream 吐一条 AI 消息,不真调模型。"""

    async def astream(self, *args, config: RunnableConfig, **kwargs):
        yield "messages", (AIMessage(content="你好!"), {})
        yield "updates", {"agent": {"messages": []}}

    async def aget_state(self, config: RunnableConfig):
        return None  # 总是"新会话"


def auth_ok_data(
    user_id: int = 7, role: str = "candidate", role_names: list[str] | None = None
) -> dict:
    """ResolvedIdentity 形状(不经 HTTP 的 resolve 返回值)。"""
    return {
        "user_id": user_id,
        "role": role,
        "role_names": role_names or ["申请人"],
        "permission_codes": ["candidate:read:own"],
        "source": "web",
    }


def fake_resolve(identity: dict):
    async def _impl(*_a: object, **_k: object) -> dict:
        return identity

    return _impl


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *identities: dict,
) -> None:
    from official_agent.web import routes

    if identities:
        responses = iter(identities)

        async def _resolve_sequential(*_a: object, **_k: object) -> dict:
            return next(responses)

        monkeypatch.setattr(routes, "resolve", _resolve_sequential)
    else:
        monkeypatch.setattr(routes, "resolve", fake_resolve(auth_ok_data()))
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: _FakeAgent())


def _sse_events(resp) -> list[dict]:
    body = "".join(resp.iter_text())
    return [
        json.loads(line[5:]) for line in body.splitlines() if line.startswith("data: ")
    ]


def test_chat_without_token_returns_401(client: TestClient) -> None:
    resp = client.post("/api/agent/chat", json={"message": "你好"})
    assert resp.status_code == 401


def test_chat_empty_message_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fakes(monkeypatch)
    resp = client.post(
        "/api/agent/chat", json={"message": "  "}, headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 400


def test_chat_auth_me_not_implemented_returns_501(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后端 /auth/me 未落地:#89 护栏拒绝,绝不放开模拟身份。"""

    async def _raise(*_a: object, **_k: object):
        raise NotImplementedError("官网通道身份解析待 /auth/me,见 issue #52")

    from official_agent.web import routes

    monkeypatch.setattr(routes, "resolve", _raise)
    resp = client.post(
        "/api/agent/chat", json={"message": "你好"}, headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 501


def test_chat_valid_token_streams_session_and_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合法 token → SSE 流返回 session_id + done。"""
    _install_fakes(monkeypatch)
    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "我的面试安排"},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        assert resp.status_code == 200
        events = _sse_events(resp)
    types = [e["type"] for e in events]
    assert "session_id" in types
    assert "done" in types
    session_id = next(e["session_id"] for e in events if e["type"] == "session_id")
    assert session_id.startswith("web:u7:")


def test_chat_resume_same_session_reuses_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """续传带原 session_id:同一 agent/thread 被复用(SSE 首条回显 session_id)。"""
    _install_fakes(monkeypatch)
    with client.stream(
        "POST", "/api/agent/chat", json={"message": "hi"}, headers={"Authorization": "Bearer tok"}
    ) as resp:
        events = _sse_events(resp)
    sid = next(e["session_id"] for e in events if e["type"] == "session_id")

    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "hi again", "session_id": sid},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        events2 = _sse_events(resp)
    sid2 = next(e["session_id"] for e in events2 if e["type"] == "session_id")
    assert sid2 == sid  # 续传同 thread,不新开


def test_chat_other_user_same_session_returns_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一 session_id 被不同 user 访问 → 403(SEC-07 属主校验)。"""
    _install_fakes(monkeypatch, auth_ok_data(user_id=7), auth_ok_data(user_id=8))
    with client.stream(
        "POST", "/api/agent/chat", json={"message": "hi"}, headers={"Authorization": "Bearer tok"}
    ) as resp:
        events = _sse_events(resp)
    sid = next(e["session_id"] for e in events if e["type"] == "session_id")

    # 用户 8 带用户 7 的 session → 403
    resp = client.post(
        "/api/agent/chat",
        json={"message": "hi", "session_id": sid},
        headers={"Authorization": "Bearer tok2"},
    )
    assert resp.status_code == 403
