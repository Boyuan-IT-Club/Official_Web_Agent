"""INF-03 CLI 单测:CliRunner 全链路(fake model)+身份失败路径+历史累积。"""

import asyncio
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

import boyuan_agent.cli as cli_mod
from boyuan_agent.cli import app
from boyuan_agent.config import Settings
from boyuan_agent.tools import readonly
from boyuan_agent.tools.client import BackendClient

runner = CliRunner()
BASE = "http://backend.test"
LOGIN = f"{BASE}/api/auth/login"


class _FakeGraphAgent:
    """模拟 create_agent 图:messages+updates 双模式流,吐出预设终答。"""

    tools: list = []

    def __init__(self, final_text: str) -> None:
        self._final = final_text

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
        from boyuan_agent.tools.client import BackendAuthError

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

    def fake_build(identity, user_token=""):  # noqa: ANN001, ARG001
        captured["identity"] = identity
        captured["user_token"] = user_token
        return fake

    monkeypatch.setattr(cli_mod, "build_assistant_agent", fake_build)

    result = runner.invoke(app, ["chat", "--session", "qa-1"], input="现在有开放周期吗?\n退出\n")

    assert result.exit_code == 0
    assert "身份=admin" in result.output
    assert "session=qa-1" in result.output
    assert "工具=" in result.output
    assert "招新助理" in result.output  # 终答流式打印
    assert "退出" in result.output
    # 身份解析成功且 token 传给了装配(candidate 工具绑定)
    assert captured["identity"]["user_id"] == 7
    assert captured["identity"]["role"] == "admin"
    assert captured["user_token"]  # login 后有 token
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
    from boyuan_agent.cli import _EXIT_WORDS

    assert "exit" in _EXIT_WORDS and "退出" in _EXIT_WORDS
    assert cast(bool, "q" in _EXIT_WORDS)


@respx.mock
def test_chat_backend_unreachable_exits_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAJOR-2 回归:后端不可达(ConnectError)也走人话,不裸栈。"""
    respx.post(LOGIN).mock(side_effect=httpx.ConnectError("refused"))
    _install_mock_backend()

    async def fake_resolve(credential):  # noqa: ANN001, ARG001
        from boyuan_agent.graphs.identity import resolve as _r

        await _r({"kind": "cli"})  # type: ignore[typeddict-item]

    monkeypatch.setattr(cli_mod, "resolve", fake_resolve)
    result = runner.invoke(app, ["chat", "--username", "x", "--password", "y"])
    assert result.exit_code == 1
    assert "身份解析失败" in result.output
    readonly.set_backend_client(None)
