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
import types
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


def test_chat_injects_identity_every_round_including_resume(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 #114:身份消息每轮注入(含续传轮,决策 #108 质量优先)。

    新建轮与续传轮的 agent 输入都必须以身份消息开头——续传轮也不例外,
    保证压缩掉早期上下文后身份边界仍在最近窗口内。
    """
    from official_agent.web import routes

    seen_inputs: list[list] = []

    class _RecordingAgent:
        async def astream(self, inp, config=None, **kwargs):
            seen_inputs.append(list(inp["messages"]))
            yield "messages", (AIMessage(content="好的"), {})
            yield "updates", {"agent": {"messages": []}}

        async def aget_state(self, config):
            return None

    agent = _RecordingAgent()
    _install_fakes(monkeypatch)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: agent)

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
        _sse_events(resp)

    assert len(seen_inputs) == 2
    for messages in seen_inputs:
        assert "当前对话用户是" in messages[0].content, "每轮输入首条必须是身份消息"
        assert messages[-1].content in ("hi", "hi again")


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


def _stateful_agent(updates: list, state_messages: list | None):
    """带 checkpoint 状态的假 agent:astream 吐一条消息,可查/可写状态。"""

    class _StatefulAgent:
        async def astream(self, inp, config=None, **kwargs):
            yield "messages", (AIMessage(content="答"), {})
            yield "updates", {"agent": {"messages": []}}

        async def aget_state(self, config):
            return types.SimpleNamespace(values={"messages": state_messages or []})

        async def update_state(self, config, values):
            updates.append(list(values["messages"]))

    return _StatefulAgent()


def test_chat_compresses_long_state_and_logs_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 #114:轮末超阈值压缩——摘要回写 checkpoint 新版本,事件随行落日志。"""
    from langchain_core.messages import HumanMessage, RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    from official_agent.graphs.assistant.compression import CompressionResult
    from official_agent.web import routes

    updates: list[list] = []
    logged: list[dict] = []
    monkeypatch.setattr(routes, "_log_conversation", lambda *a, **k: logged.append(k))
    _install_fakes(monkeypatch)
    monkeypatch.setattr(
        routes,
        "build_assistant_agent",
        lambda *a, **k: _stateful_agent(updates, [HumanMessage("旧" * 50), AIMessage("旧答")]),
    )

    async def _fake_compress(*_a: object, **_k: object) -> CompressionResult:
        return CompressionResult(
            new_messages=[HumanMessage("[历史摘要] 摘要"), HumanMessage("近轮")],
            trigger_tokens=31500,
            covered=38,
            summary_tokens=812,
        )

    monkeypatch.setattr(routes, "maybe_compress", _fake_compress)

    with client.stream(
        "POST", "/api/agent/chat", json={"message": "hi"}, headers={"Authorization": "Bearer tok"}
    ) as resp:
        events = _sse_events(resp)
    assert any(e["type"] == "done" for e in events)
    assert len(updates) == 1
    # 回写形状:先全删(产生新版本),再加「摘要 + 近几轮」;旧版本仍可回溯
    first = updates[0][0]
    assert isinstance(first, RemoveMessage) and first.id == REMOVE_ALL_MESSAGES
    assert updates[0][1].content.startswith("[历史摘要]")
    assert logged[0]["compress_event"] == (
        "trigger_tokens=31500;covered=38;kept=1;summary_tokens=812"
    )


def test_chat_no_compression_under_threshold(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 #114:状态未超阈值 → 不压缩不回写,compress_event 为 None。"""
    from official_agent.web import routes

    updates: list[list] = []
    logged: list[dict] = []
    monkeypatch.setattr(routes, "_log_conversation", lambda *a, **k: logged.append(k))
    _install_fakes(monkeypatch)
    monkeypatch.setattr(
        routes,
        "build_assistant_agent",
        lambda *a, **k: _stateful_agent(updates, [AIMessage("短")]),
    )

    with client.stream(
        "POST", "/api/agent/chat", json={"message": "hi"}, headers={"Authorization": "Bearer tok"}
    ) as resp:
        events = _sse_events(resp)
    assert any(e["type"] == "done" for e in events)
    assert updates == []
    assert logged[0]["compress_event"] is None


def test_chat_compression_failure_fail_open(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 #114:压缩失败不影响本轮对话(fail-open,ADR-0005)。"""
    from official_agent.web import routes

    updates: list[list] = []
    logged: list[dict] = []
    monkeypatch.setattr(routes, "_log_conversation", lambda *a, **k: logged.append(k))
    _install_fakes(monkeypatch)
    monkeypatch.setattr(
        routes,
        "build_assistant_agent",
        lambda *a, **k: _stateful_agent(updates, [AIMessage("旧" * 50)]),
    )

    async def _boom(*_a: object, **_k: object):
        raise RuntimeError("压缩炸了")

    monkeypatch.setattr(routes, "maybe_compress", _boom)

    with client.stream(
        "POST", "/api/agent/chat", json={"message": "hi"}, headers={"Authorization": "Bearer tok"}
    ) as resp:
        events = _sse_events(resp)
    assert any(e["type"] == "done" for e in events)
    assert updates == []
    assert logged[0]["compress_event"] is None


def test_chat_writes_conversation_log_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一轮 /chat 在 conversation_log 落一行(#110):_log_conversation 被调用。

    _log_conversation 内部 fire-and-forget(异步写,不阻塞流),此处 patch 它
    同步捕获调用以验证「每轮落一行 + 字段齐全」;PII 过滤在数据层
    (test_state_conversation 单测覆盖)。
    """
    from official_agent.web import routes

    logged: list[dict] = []

    def _fake_log(*_args, **kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(routes, "_log_conversation", _fake_log)
    _install_fakes(monkeypatch)
    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "我的电话是 13812345678"},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        events = _sse_events(resp)
    assert resp.status_code == 200
    assert any(e["type"] == "done" for e in events)
    assert logged, "_log_conversation 未被调用"
    assert len(logged) == 1  # 一轮只落一行(单一写入路径)
    row = logged[0]
    assert row["user_message"] == "我的电话是 13812345678"
    assert row["duration_ms"] >= 0
    assert row["error_code"] is None  # 正常轮无错误码




def test_chat_error_path_logs_error_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent 抛异常 → error 事件 + conversation_log 落 error_code 行(#110)。"""
    from official_agent.web import routes

    class _FailingAgent:
        async def astream(self, *args, **kwargs):
            """async generator:首次迭代即抛(模拟 agent 执行期异常)。"""
            if False:
                yield None
            raise RuntimeError("boom")

        async def aget_state(self, config):
            return None

    logged: list[dict] = []

    def _fake_log(*_args, **kwargs):
        logged.append(kwargs)

    monkeypatch.setattr(routes, "_log_conversation", _fake_log)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: _FailingAgent())
    monkeypatch.setattr(routes, "resolve", fake_resolve(auth_ok_data()))
    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "你好"},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        events = _sse_events(resp)
    assert resp.status_code == 200
    assert any(e["type"] == "error" for e in events)
    assert logged, "_log_conversation 未被调用"
    assert len(logged) == 1
    assert logged[0]["error_code"] == "unknown"  # RuntimeError → unknown
    assert logged[0]["reply_summary"] == ""  # 错误行不存回复


def test_chat_rebuilds_agent_after_config_change(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M6 #111 热生效:PUT /admin/config 后,续传会话下一轮重建 agent。"""
    from official_agent.web import routes

    builds: list[dict] = []

    # 先装 fakes(resolve + 默认 fake agent);再包计数层,避免被 _install_fakes 覆盖
    _install_fakes(monkeypatch)

    def _counting_build(*args, **kwargs):
        builds.append(kwargs)
        return _FakeAgent()

    monkeypatch.setattr(routes, "build_assistant_agent", _counting_build)
    monkeypatch.setattr(routes, "_config_fingerprint", lambda: "fp-1")

    # 第一轮:新建 session,agent 构建 1 次
    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "hi"},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        events = _sse_events(resp)
    sid = next(e["session_id"] for e in events if e["type"] == "session")
    assert len(builds) == 1

    # 配置变化(指纹变)→ 下一轮重建
    monkeypatch.setattr(routes, "_config_fingerprint", lambda: "fp-2")
    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "hi again", "session_id": sid},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        _sse_events(resp)
    assert len(builds) == 2  # 配置变更后重建

    # 指纹不变 → 不重建
    with client.stream(
        "POST",
        "/api/agent/chat",
        json={"message": "hi 3", "session_id": sid},
        headers={"Authorization": "Bearer tok"},
    ) as resp:
        _sse_events(resp)
    assert len(builds) == 2
