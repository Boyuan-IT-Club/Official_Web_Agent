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
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data: ")]


def test_error_code_classification() -> None:
    """执行期异常 → 契约错误码(issue #90)。"""
    import httpx

    from official_agent.tools.client import BackendError
    from official_agent.web.routes import _error_code

    assert _error_code(BackendError("用户令牌无效或已过期,需用户重新登录后重试")) == "auth_expired"
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


def test_chat_bad_token_returns_401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_chat_message_face_carries_no_identity_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#166 回归:身份/权限上下文绝不进对话消息面(否则回看泄漏成 user 气泡)。

    新建轮与续传轮的消息输入都必须只有用户原文;身份上下文只经
    build_system_prompt 进 system prompt。
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
        # 消息面只有用户原文;不得含身份/权限/工具契约
        assert len(messages) == 1, "每轮输入只应有用户原文"
        assert messages[0].content in ("hi", "hi again")
        assert "当前对话用户是" not in messages[0].content
        assert "可访问权限" not in messages[0].content
        assert "数据查询工具:" not in messages[0].content


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


def test_chat_usage_from_usage_metadata_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#115 实测回归:langchain-openai 1.x 流式只有 usage_metadata
    (response_metadata.token_usage 已消失)→ token 仍须正确落行。"""
    from langchain_core.messages import AIMessageChunk

    from official_agent.web import routes

    class _UsageAgent:
        async def astream(self, inp, config=None, **kwargs):
            yield (
                "messages",
                (
                    AIMessageChunk(
                        content="你好",
                        usage_metadata={
                            "input_tokens": 87,
                            "output_tokens": 22,
                            "total_tokens": 109,
                            "input_token_details": {"cache_read": 12, "cache_creation": 75},
                        },
                    ),
                    {},
                ),
            )
            yield "updates", {"agent": {"messages": []}}

        async def aget_state(self, config):
            return types.SimpleNamespace(values={"messages": [AIMessage("短")]})

        async def update_state(self, config, values):
            pass

    logged: list[dict] = []
    monkeypatch.setattr(routes, "_log_conversation", lambda *a, **k: logged.append(k))
    _install_fakes(monkeypatch)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: _UsageAgent())

    with client.stream(
        "POST", "/api/agent/chat", json={"message": "hi"}, headers={"Authorization": "Bearer tok"}
    ) as resp:
        events = _sse_events(resp)
    assert any(e["type"] == "done" for e in events)

    usage = logged[0]["usage"]
    assert usage["input_tokens"] == 87
    assert usage["output_tokens"] == 22
    assert usage["cache_hit_tokens"] == 12
    assert usage["cache_miss_tokens"] == 75


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
        "turn=1;trigger_tokens=31500;covered=38;kept=1;summary_tokens=812"
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
    from official_agent.graphs.assistant.compression import (
        record_compression_success,
    )
    from official_agent.web import routes

    record_compression_success()  # 隔离其他用例留下的熔断计数
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


def test_compression_circuit_breaker_pauses_after_repeated_failures() -> None:
    """M6 #114:连续失败达阈值 → 熔断暂停尝试(ADR-0004),成功后复位。"""
    from official_agent.graphs.assistant import compression as comp

    comp.record_compression_success()
    for _ in range(comp._MAX_CONSECUTIVE_FAILURES - 1):
        comp.record_compression_failure()
    assert not comp.compression_paused()
    comp.record_compression_failure()
    assert comp.compression_paused()  # 达阈值:暂停尝试
    comp.record_compression_success()
    assert not comp.compression_paused()  # 成功复位


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


def test_chat_message_too_long_returns_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review P0-2:单条消息超长 → 400(收敛单请求滥用面;限流参数归 #56)。"""
    from official_agent.web import routes

    _install_fakes(monkeypatch)
    long_msg = "长" * (routes._MAX_MESSAGE_CHARS + 1)
    resp = client.post(
        "/api/agent/chat",
        json={"message": long_msg},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 400
    assert "消息过长" in resp.text


def test_stream_turn_busy_when_lock_held(client: TestClient) -> None:
    """review P0-1:同会话并发轮次——锁被占用 → 立即回 busy,不再进 astream。"""
    import asyncio
    import json as jsonlib

    from official_agent.web import routes

    identity = auth_ok_data()
    calls: list = []

    class _HangAgent:
        async def astream(self, inp, config=None, **kwargs):  # pragma: 不应被调用
            calls.append(1)
            yield "messages", (AIMessage(content="不应出现"), {})
            yield "updates", {"agent": {"messages": []}}

        async def aget_state(self, config):
            return None

    session = routes._SessionState("web:u7:busyt1", identity, "tok", _HangAgent())
    session.applied_config_fingerprint = routes._config_fingerprint()

    async def run():
        async with session.turn_lock:  # 模拟另一轮正在执行
            gen = routes._stream_turn(session, "hi", False)
            first = await gen.__anext__()
            event = jsonlib.loads(first[len("data: ") :])
            assert event["type"] == "error" and event["code"] == "busy"
            await gen.aclose()

    asyncio.run(run())
    assert calls == []  # busy 轮未触达模型


def test_get_resume_detail_masks_pii_in_payload() -> None:
    """review P0-3:简历详情返回层就地脱敏——trace 上报的是脱敏后数据。"""
    import asyncio

    from official_agent.tools import readonly

    class _FakeClient:
        async def get(self, url):
            return {
                "resume": {"resume_id": 1},
                "fieldValues": [
                    {"fieldKey": "phone", "fieldValue": "13812345678"},
                    {"fieldKey": "extra_id", "fieldValue": "310101199001011234"},
                    {"fieldKey": "intro", "fieldValue": "我的 QQ 是 123456789"},
                ],
            }

    readonly.set_backend_client(_FakeClient())

    async def run():
        return await readonly.get_resume_detail(7, 3)

    result = asyncio.run(run())
    values = [fv["fieldValue"] for fv in result["fieldValues"]]
    assert values[0] == "138****5678"
    assert values[1] == "3101**********1234"
    assert "123456789" not in values[2]
    readonly.set_backend_client(None)


# ── 编造守卫与工具契约(#161,GRA-04) ─────────────────────


def _install_fake_agent(monkeypatch: pytest.MonkeyPatch, reply: str) -> list:
    """装一个吐固定回复的假 agent,并捕获 astream 收到的消息。"""
    from official_agent.web import routes

    seen: list = []

    from langchain_core.messages import AIMessageChunk

    class _ScriptedAgent:
        async def astream(self, inputs, config: RunnableConfig, **kwargs):
            seen.extend(inputs["messages"])
            yield "messages", (AIMessageChunk(content=reply), {})
            yield "updates", {"agent": {"messages": []}}

        async def aget_state(self, config: RunnableConfig):
            return None

    monkeypatch.setattr(routes, "resolve", fake_resolve(auth_ok_data()))
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: _ScriptedAgent())
    return seen


def test_chat_toolless_reply_buffered_and_guarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GRA-04 原案:unknown 档 tools=[],模型编造「查询结果」→ 不见原 delta,
    流尾守卫整段改写后一次性下发。"""
    _install_fake_agent(monkeypatch, "查询结果:您有 3 份简历待筛选。")
    ident = auth_ok_data(role="unknown", role_names=["访客"])
    ident["permission_codes"] = []
    monkeypatch.setattr("official_agent.web.routes.resolve", fake_resolve(ident))
    resp = client.post(
        "/api/agent/chat", json={"message": "有几份简历?"}, headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    deltas = [e["content"] for e in events if e["type"] == "delta"]
    assert deltas == [
        "我没有可用的数据查询权限,无法查询系统数据。"
        "如需查询简历、面试安排或统计信息,请登录对应系统或联系管理员处理。"
    ]


def test_chat_first_message_is_user_content_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#166:首条消息就是用户原文;工具契约在 system(不进消息面)。"""
    seen = _install_fake_agent(monkeypatch, "好的。")
    resp = client.post(
        "/api/agent/chat", json={"message": "在吗"}, headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 200
    first_content = seen[0].content
    assert first_content == "在吗"
    assert "数据查询工具:" not in first_content


def test_chat_toolless_honest_reply_passes_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """toolless 缓冲 + 诚实话术:守卫 clean,缓冲内容原样单条下发。"""
    _install_fake_agent(monkeypatch, "我没有查询权限,请联系管理员。")
    ident = auth_ok_data(role="unknown", role_names=["访客"])
    ident["permission_codes"] = []
    monkeypatch.setattr("official_agent.web.routes.resolve", fake_resolve(ident))
    resp = client.post(
        "/api/agent/chat", json={"message": "在吗"}, headers={"Authorization": "Bearer tok"}
    )
    events = _sse_events(resp)
    deltas = [e["content"] for e in events if e["type"] == "delta"]
    assert deltas == ["我没有查询权限,请联系管理员。"]


def test_guard_rewrite_persists_to_checkpointer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """守卫回写成功路径:末条 AI 消息被 RemoveMessage+改写文本替换(#161 复审 P2)。"""
    from langchain_core.messages import AIMessage, AIMessageChunk, RemoveMessage

    from official_agent.web import routes

    seen_messages: list = []

    class _StatefulAgent:
        async def astream(self, inputs, config: RunnableConfig, **kwargs):
            yield "messages", (AIMessageChunk(content="查询结果:有 5 份简历。"), {})
            yield "updates", {"agent": {"messages": []}}

        async def aget_state(self, config: RunnableConfig):
            class _State:
                values = {"messages": [AIMessage(content="查询结果:有 5 份简历。", id="ai-1")]}

            return _State()

        async def aupdate_state(self, config: RunnableConfig, update: dict, **kwargs):
            seen_messages.extend(update["messages"])

    monkeypatch.setattr(routes, "resolve", fake_resolve(auth_ok_data(role="unknown")))
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: _StatefulAgent())
    resp = client.post(
        "/api/agent/chat", json={"message": "有几份简历?"}, headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 200
    assert len(seen_messages) == 2
    assert isinstance(seen_messages[0], RemoveMessage) and seen_messages[0].id == "ai-1"
    assert "没有可用的数据查询权限" in seen_messages[1].content
