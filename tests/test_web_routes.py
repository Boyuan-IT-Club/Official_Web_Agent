"""INF-04 SSE 聊天路由测试:鉴权/会话隔离。

用 FastAPI TestClient,验证:
- 无 Authorization → 401
- 坏 token(身份解析失败)→ 401
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




def test_error_code_classification() -> None:
    """执行期异常 → 契约错误码(issue #90)。"""
    import httpx

    from official_agent.tools.client import BackendError
    from official_agent.web.routes import _error_code

    assert (
        _error_code(BackendError("用户令牌无效或已过期,需用户重新登录后重试"))
        == "auth_expired"
    )
    assert _error_code(BackendError("token 无效")) == "auth_expired"
    assert _error_code(httpx.ConnectError("refused")) == "backend_unavailable"
    assert _error_code(httpx.TimeoutException("slow")) == "backend_unavailable"
    # 非 auth 的后端业务错误(如「未投递」)→ invalid_request
    assert _error_code(BackendError("该周期未开放投递")) == "invalid_request"
    assert _error_code(RuntimeError("boom")) == "unknown"


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


def test_chat_bad_token_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """身份解析失败(后端 /auth/me 拒/不可达)→ 401,不透出异常细节。"""

    async def _fail(*_a: object, **_k: object):
        from official_agent.tools.client import BackendError

        raise BackendError("用户令牌无效或已过期,需用户重新登录后重试")

    from official_agent.web import routes

    monkeypatch.setattr(routes, "resolve", _fail)
    resp = client.post(
        "/api/agent/chat", json={"message": "你好"}, headers={"Authorization": "Bearer bad"}
    )
    assert resp.status_code == 401
    # review:不透出内网/异常细节,只回通用文案
    assert "身份解析失败" in resp.text
    assert "backend" not in resp.text.lower()




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
    assert "session" in types
    assert "done" in types
    session_id = next(e["session_id"] for e in events if e["type"] == "session")
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
    sid = next(e["session_id"] for e in events if e["type"] == "session")

    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "hi again", "session_id": sid},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        events2 = _sse_events(resp)
    sid2 = next(e["session_id"] for e in events2 if e["type"] == "session")
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
    sid = next(e["session_id"] for e in events if e["type"] == "session")

    # 用户 8 带用户 7 的 session → 403
    resp = client.post(
        "/api/agent/chat",
        json={"message": "hi", "session_id": sid},
        headers={"Authorization": "Bearer tok2"},
    )
    assert resp.status_code == 403
