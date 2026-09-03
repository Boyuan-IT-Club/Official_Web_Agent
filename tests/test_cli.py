"""INF-03 CLI 单测:CliRunner 全链路(fake model)+身份失败路径+历史累积。"""

import asyncio
import re
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
import respx
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from typer.testing import CliRunner

import official_agent.cli as cli_mod
from official_agent.cli import app
from official_agent.config import Settings
from official_agent.tools import readonly
from official_agent.tools.client import BackendClient

runner = CliRunner()
BASE = "http://backend.test"
LOGIN = f"{BASE}/api/auth/login"


class _FakeGraphAgent:
    """模拟 create_agent 图:messages+updates 双模式流,吐出预设终答。"""

    tools: list = []

    def __init__(self, final_text: str) -> None:
        self._final = final_text

    async def aget_state(self, config: dict | None = None) -> dict | None:  # noqa: ANN201, ARG002
        """模拟 checkpointer 状态查询:默认无历史(新线程)。"""
        return None

    async def astream(self, inp: dict, config: dict | None = None, stream_mode=None):  # noqa: ANN201
        assert stream_mode == ["messages", "updates"]
        assert config["configurable"]["thread_id"]
        # 契约对齐真实 create_agent:messages 吐 chunk;updates 每节点只吐增量
        yield "messages", (AIMessageChunk(content=self._final[:3]), {})
        yield "messages", (AIMessageChunk(content=self._final[3:]), {})
        yield "updates", {"agent": {"messages": [AIMessage(self._final)]}}


class _FakeToolModel(BaseChatModel):
    messages: Iterator[Any]

    @property
    def _llm_type(self) -> str:
        return "fake-cli"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeToolModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=next(self.messages))])


def _login_ok(user_id: int = 7, role: str = "管理员") -> httpx.Response:
    import base64
    import json

    def b64(seg: bytes) -> str:
        return base64.urlsafe_b64encode(seg).decode().rstrip("=")

    claims = {"userId": user_id, "roleNames": [role], "permissionCodes": ["user:view"]}
    token = f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}.sig"
    return httpx.Response(
        200, json={"code": 200, "message": "ok", "data": {"token": token}}
    )


def _install_mock_backend() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        backend_base_url=BASE,
        backend_service_username="svc",
        backend_service_password="secret",
    )
    readonly.set_backend_client(
        BackendClient(http=httpx.AsyncClient(base_url=BASE), settings=settings)
    )


@respx.mock
def test_chat_bad_credentials_exits_with_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """身份解析失败:友好报错+退出码 1,不栈溢出到用户脸上。"""
    respx.post(LOGIN).mock(
        side_effect=lambda _: httpx.Response(401, json={"code": 401, "message": "用户名或密码错误"})
    )
    _install_mock_backend()

    async def fake_resolve(credential):  # noqa: ANN001, ARG001
        from official_agent.tools.client import BackendAuthError

        raise BackendAuthError("用户名或密码错误(code 401)")

    monkeypatch.setattr(cli_mod, "resolve", fake_resolve)
    result = runner.invoke(app, ["chat", "--username", "x", "--password", "y"])
    assert result.exit_code == 1
    assert "身份解析失败" in result.output


@respx.mock
def test_chat_full_roundtrip_with_fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 全链路:身份解析 → agent 构建(fake model) → 多轮输入 → 流式输出终答。"""
    respx.post(LOGIN).mock(side_effect=lambda _: _login_ok())
    _install_mock_backend()

    captured: dict = {}
    fake = _FakeGraphAgent("你好,我是招新助理,当前没有开放周期数据。")

    def fake_build(identity, user_token="", checkpointer=None):  # noqa: ANN001, ARG001
        captured["identity"] = identity
        captured["user_token"] = user_token
        captured["checkpointer"] = checkpointer
        return fake

    monkeypatch.setattr(cli_mod, "build_assistant_agent", fake_build)

    result = runner.invoke(app, ["chat", "--session", "qa-1"], input="现在有开放周期吗?\n退出\n")

    assert result.exit_code == 0
    assert "身份=admin" in result.output
    # SEC-07:session 显示为生成的 thread_id(cli:u{user}:{random8}),不是别名
    assert re.search(r"session=cli:u7:[0-9a-f]{8}", result.output)
    assert "工具=" in result.output
    assert "招新助理" in result.output  # 终答流式打印
    assert "退出" in result.output
    # 身份解析成功且 token 传给了装配(candidate 工具绑定)
    assert captured["identity"]["user_id"] == 7
    assert captured["identity"]["role"] == "admin"
    assert captured["user_token"]  # login 后有 token
    readonly.set_backend_client(None)


@respx.mock
def test_chat_degraded_no_pg_keeps_multi_turn_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """降级路径(PG 不可达 → saver=None):多轮对话仍用本地历史,不失忆。

    回归:最初接 checkpointer 时删了本地 history 累积,saper=None 时第二轮
    agent 收不到第一轮输入(多轮失忆)。
    """
    respx.post(LOGIN).mock(side_effect=lambda _: _login_ok())
    _install_mock_backend()

    seen_inputs: list[list] = []

    class _CapturingAgent(_FakeGraphAgent):
        async def astream(self, inp: dict, config: dict | None = None, stream_mode=None):  # noqa: ANN201, ARG002
            seen_inputs.append(list(inp["messages"]))
            async for item in super().astream(inp, config, stream_mode):
                yield item

    captured: dict = {}

    def fake_build(identity, user_token="", checkpointer=None):  # noqa: ANN001, ARG001
        captured["identity"] = identity
        captured["checkpointer"] = checkpointer
        return _CapturingAgent("好的")

    monkeypatch.setattr(cli_mod, "build_assistant_agent", fake_build)

    # PG 不可达:get_checkpointer 抛异常 → 降级 saver=None
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def boom():
        raise ConnectionError("connection refused")
        yield None  # pragma: no cover — unreachable

    monkeypatch.setattr(cli_mod, "get_checkpointer", boom)

    result = runner.invoke(
        app, ["chat", "--session", "qa-1"], input="第一轮问题\n第二轮追问\n退出\n"
    )
    assert result.exit_code == 0
    assert captured["checkpointer"] is None  # 降级生效
    assert len(seen_inputs) == 2
    # 第二轮必须包含第一轮用户输入(本地历史累积)
    first_round_msgs = [m.content for m in seen_inputs[0]]
    second_round_msgs = [m.content for m in seen_inputs[1]]
    assert any("第一轮问题" in str(c) for c in first_round_msgs)
    assert any("第一轮问题" in str(c) for c in second_round_msgs)
    assert any("第二轮追问" in str(c) for c in second_round_msgs)
    readonly.set_backend_client(None)


@respx.mock
def test_chat_resume_existing_thread_no_duplicate_identity_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-1 回归:续接已有线程(aget_state 有历史)只发增量,不重复身份前缀。

    跨进程续接是本功能核心;进程局部 turn 计数会误判"首轮"给已有线程
    再注入身份前缀。fix 后判据改为持久化事实(aget_state)。
    """
    respx.post(LOGIN).mock(side_effect=lambda _: _login_ok())
    _install_mock_backend()

    seen_inputs: list[list] = []

    class _ResumeAgent(_FakeGraphAgent):
        def __init__(self) -> None:
            super().__init__("ok")
            self._call = 0

        async def aget_state(self, config: dict | None = None):  # noqa: ANN201, ARG002
            # 真实契约:StateSnapshot 对象,含 .values dict(已有历史+身份前缀)
            from types import SimpleNamespace

            return SimpleNamespace(values={"messages": [HumanMessage("已存在的身份前缀")]})

        async def astream(self, inp: dict, config: dict | None = None, stream_mode=None):  # noqa: ANN201, ARG002
            seen_inputs.append(list(inp["messages"]))
            async for item in super().astream(inp, config, stream_mode):
                yield item

    def fake_build(identity, user_token="", checkpointer=None):  # noqa: ANN001, ARG001
        return _ResumeAgent()

    monkeypatch.setattr(cli_mod, "build_assistant_agent", fake_build)

    result = runner.invoke(app, ["chat", "--session", "qa-1"], input="续接问题\n退出\n")

    assert result.exit_code == 0
    assert len(seen_inputs) == 1
    msgs = seen_inputs[0]
    # 续接时只发用户输入,不带身份前缀
    assert not any(m.content == "已存在的身份前缀" for m in msgs)
    assert any(m.content == "续接问题" for m in msgs)
    readonly.set_backend_client(None)


@respx.mock
def test_chat_first_round_failure_reinjects_identity_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-2 回归:首轮失败(未持久化)→ 第二轮自动重发身份前缀。

    修复前 turn 单调递增,失败轮丢失前缀后永不恢复;修复后判据是
    aget_state 无历史 → 失败轮后续自动重发。
    """
    respx.post(LOGIN).mock(side_effect=lambda _: _login_ok())
    _install_mock_backend()

    seen_inputs: list[list] = []

    class _FlakyAgent(_FakeGraphAgent):
        def __init__(self) -> None:
            super().__init__("ok")
            self._call = 0
            self._fail_rounds = {0}  # 第一轮失败

        async def aget_state(self, config: dict | None = None) -> dict | None:  # noqa: ANN201, ARG002
            return None  # 始终无历史(失败轮未持久化)

        async def astream(self, inp: dict, config: dict | None = None, stream_mode=None):  # noqa: ANN201, ARG002
            if self._call in self._fail_rounds:
                self._call += 1
                raise RuntimeError("模拟首轮失败")
            self._call += 1
            seen_inputs.append(list(inp["messages"]))
            async for item in super().astream(inp, config, stream_mode):
                yield item

    def fake_build(identity, user_token="", checkpointer=None):  # noqa: ANN001, ARG001
        return _FlakyAgent()

    monkeypatch.setattr(cli_mod, "build_assistant_agent", fake_build)

    result = runner.invoke(app, ["chat", "--session", "qa-1"], input="第一问\n第二问\n退出\n")

    assert result.exit_code == 0
    assert len(seen_inputs) == 1  # 第二问成功,第一问失败
    msgs = seen_inputs[0]
    # 第二轮重新带上了身份前缀(首轮失败未持久化 → 仍视为新线程)
    assert any(getattr(m, "type", "") == "human" and "身份" in str(m.content) for m in msgs)
    assert any("第二问" in str(m.content) for m in msgs)
    readonly.set_backend_client(None)


def test_run_turn_accumulates_history_and_shows_tool_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """单轮执行:流式打 token,updates 模式回收完整历史(含工具消息)。"""
    history = [HumanMessage("[会话身份] 管理员(用户 7)"), HumanMessage("查周期")]

    class FakeWithTools:
        """带工具消息的图替身:流含 tool_call/tool 消息,验证历史累积与显示。"""

        tools: list = []

        async def astream(self, inp, config=None, stream_mode=None):  # noqa: ANN001, ANN202
            assert stream_mode == ["messages", "updates"]
            yield "messages", (AIMessageChunk(content="查询中"), {})
            # 三节点各吐增量(model→tools→model),对齐真实图契约
            yield "updates", {"agent": {"messages": [
                AIMessage("", tool_calls=[{"name": "get_open_cycle", "args": {}, "id": "c1"}]),
            ]}}
            yield "updates", {"tools": {"messages": [
                ToolMessage("{'cycleId': 2}", tool_call_id="c1", name="get_open_cycle"),
            ]}}
            yield "updates", {"agent": {"messages": [AIMessage("当前 1 个开放周期。")]}}

    async def go() -> list:
        return await cli_mod._run_turn(FakeWithTools(), history, "s1", [])

    out = asyncio.run(go())
    assert out[-1].content == "当前 1 个开放周期。"
    assert any(isinstance(m, ToolMessage) and m.name == "get_open_cycle" for m in out)
    # 历史含原始输入(累积语义)
    assert any(isinstance(m, HumanMessage) and m.content == "查周期" for m in out)


def test_exit_words_and_eof() -> None:
    """退出词/EOF 干净退出。"""
    from official_agent.cli import _EXIT_WORDS

    assert "exit" in _EXIT_WORDS and "退出" in _EXIT_WORDS
    assert cast(bool, "q" in _EXIT_WORDS)


@respx.mock
def test_chat_backend_unreachable_exits_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAJOR-2 回归:后端不可达(ConnectError)也走人话,不裸栈。"""
    respx.post(LOGIN).mock(side_effect=httpx.ConnectError("refused"))
    _install_mock_backend()

    async def fake_resolve(credential):  # noqa: ANN001, ARG001
        from official_agent.graphs.identity import resolve as _r

        await _r({"kind": "cli"})  # type: ignore[typeddict-item]

    monkeypatch.setattr(cli_mod, "resolve", fake_resolve)
    result = runner.invoke(app, ["chat", "--username", "x", "--password", "y"])
    assert result.exit_code == 1
    assert "身份解析失败" in result.output
    readonly.set_backend_client(None)
