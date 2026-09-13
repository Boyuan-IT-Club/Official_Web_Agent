"""GRA-04 ReAct 单循环单测:装配三档/凭证不可见/静态前缀/工具转换/fake-model 链路。"""

import inspect
from collections.abc import Iterator  # noqa: E402
from typing import Any, cast

import pytest
import respx
from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models.chat_models import (  # noqa: E402
    BaseChatModel,
)
from langchain_core.messages import (
    AIMessage,
    BaseMessage,  # noqa: E402
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import StructuredTool

from official_agent.graphs.assistant import (
    _bind_my_interview,
    assemble_tools,
    identity_message,
    load_system_prompt,
)
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.tools import readonly


class _FakeToolCallingModel(BaseChatModel):
    """支持 bind_tools 的 fake model——create_react_agent 内部必调 bind_tools,
    库自带的 GenericFakeChatModel 未覆写(NotImplementedError)。"""

    messages: Iterator[Any]

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeToolCallingModel":  # noqa: ARG002
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=next(self.messages))])


def identity_of(role: str) -> ResolvedIdentity:
    return cast(
        ResolvedIdentity,
        {
            "user_id": 7,
            "name": "测试用户",
            "role": role,
            "role_names": [role],
            "permission_codes": [],
            "source": "cli",
        },
    )


def test_assemble_admin_gets_all_readonly_tools() -> None:
    tools = assemble_tools(identity_of("admin"))
    names = {getattr(t, "__name__", "") for t in tools}
    assert len(tools) == 9
    assert "get_open_cycle" in names and "get_candidate_card" in names


def test_assemble_member_public_query_face() -> None:
    names = {getattr(t, "__name__", "") for t in assemble_tools(identity_of("member"))}
    assert names == {
        "get_open_cycle",
        "search_resumes",
        "get_recruit_statistics",
        "find_available_sessions",
    }


def test_assemble_candidate_gets_open_cycle_and_my_interview() -> None:
    """候选入口:能取当前周期 + 查本人面试(后者绑定用户令牌)。"""
    tools = assemble_tools(identity_of("candidate"), user_token="tok")
    assert [t.__name__ for t in tools] == ["get_open_cycle", "get_my_interview"]


def test_assemble_unknown_gets_nothing() -> None:
    assert assemble_tools(identity_of("unknown")) == []
    assert assemble_tools(identity_of("whatever")) == []


def test_bound_my_interview_hides_token_from_model() -> None:
    """凭证红线:绑定后的工具签名只剩 cycle_id,token 不进模型可见面。"""
    bound = _bind_my_interview("secret-token")
    assert bound.__name__ == "get_my_interview"
    assert bound.__doc__ == readonly.get_my_interview.__doc__  # 工具描述不丢
    params = list(inspect.signature(bound).parameters)
    assert params == ["cycle_id"]
    # LangChain 工具化后的 schema 同样无 token
    tool = StructuredTool.from_function(bound, coroutine=bound)
    assert "user_token" not in tool.args
    assert "cycle_id" in tool.args


def test_all_tools_convert_to_openai_functions() -> None:
    """全部装配档位的工具都能被 LangChain 转换(签名/docstring 合法)。"""
    for role in ("admin", "member"):
        for fn in assemble_tools(identity_of(role)):
            tool = StructuredTool.from_function(fn, coroutine=fn)
            assert tool.name and tool.description


def test_system_prompt_is_role_agnostic_static_prefix() -> None:
    """静态前缀纪律:system prompt 与角色无关——角色差异只进首条用户消息。"""
    prompt = load_system_prompt()
    assert "博远" in prompt
    # 禁的是动态注入痕迹(模板变量/身份标记),不是词面——「服务对象含候选人」
    # 是静态描述,与角色无关
    for banned in ("[会话身份]", "{role}", "{user", "用户 7"):
        assert banned not in prompt, f"system 出现动态/角色内容: {banned}"


def test_identity_message_carries_role() -> None:
    msg = identity_message(identity_of("admin"))
    # 身份文案:含职位,不含内部字段(user_id/source)
    assert "管理员" in msg
    assert "7" not in msg
    assert "来源" not in msg


async def test_react_loop_with_fake_model_tool_roundtrip(monkeypatch: pytest.MonkeyPatch):
    """fake model 驱动完整 ReAct 链路:模型发起工具调用→工具真执行→结果回模型→终答。

    不花 API 费用验证 create_react_agent 接线(工具绑定/消息流/终止)。
    """

    from official_agent.graphs.assistant import build_assistant_agent

    seq = iter(
        [
            AIMessage(
                "",
                tool_calls=[{"name": "get_open_cycle", "args": {}, "id": "call_1"}],
            ),
            AIMessage("当前有 1 个开放周期:2025 秋招(cycleId=2)。"),
        ]
    )
    fake_model = _FakeToolCallingModel(messages=seq)

    async def fake_get_open_cycle() -> dict:
        """查询当前开放的招募周期。"""
        return {"cycleId": 2, "cycleName": "2025 秋招"}

    fake_get_open_cycle.__name__ = "get_open_cycle"  # 注册名与真工具一致

    import official_agent.graphs.assistant as assistant_mod

    # 强制 anthropic 假分支:本地 .env 是 openai-compatible 时不钉住 provider
    # 会构造真 ChatOpenAI → 真调 DeepSeek(历史意外路径),既慢又不确定
    class _FakeSettings:
        llm_provider = "anthropic"
        model_strong = "fake"
        anthropic_api_key = ""
        llm_base_url = ""
        llm_api_key = ""

    monkeypatch.setattr(
        assistant_mod, "get_effective_settings", lambda: _FakeSettings(), raising=True
    )

    # _ALL_TOOLS 在 import 时捕获原函数引用,patch 装配表本身
    patched_tools = dict(assistant_mod._ALL_TOOLS, get_open_cycle=fake_get_open_cycle)
    monkeypatch.setattr(assistant_mod, "_ALL_TOOLS", patched_tools, raising=True)
    monkeypatch.setattr(assistant_mod, "ChatAnthropic", lambda **kwargs: fake_model, raising=True)
    agent = build_assistant_agent(identity_of("admin"))
    result = await agent.ainvoke({"messages": [HumanMessage("现在有开放周期吗?")]})
    msgs = result["messages"]
    # 链路完整性:human → tool_call → tool result → final answer
    assert isinstance(msgs[1], AIMessage) and msgs[1].tool_calls
    assert isinstance(msgs[2], ToolMessage) and msgs[2].name == "get_open_cycle"
    assert "cycleId" in str(msgs[2].content) or "2025" in str(msgs[2].content)
    assert "2025 秋招" in msgs[-1].content


@respx.mock
async def test_react_loop_read_tool_runs_as_asker_under_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A·SEC-02 端到端:asker_scope 在 web turn 开启,经真实 LangGraph agent
    (非直调)驱动一个真只读工具(get_open_cycle → readonly._read)时,后端请求
    必须以来问者本人 JWT 身份(get_as_user),而非服务账号。

    这是对「ContextVar 从 web 层跨 agent 工具链传播」的证明——单测直调函数
    覆盖不到这段;若 LangGraph 工具执行脱离开启作用域的协程/任务,此测试必红。
    """
    import httpx

    from official_agent.config import Settings
    from official_agent.graphs.assistant import build_assistant_agent
    from official_agent.tools.client import BackendClient
    from official_agent.tools.readonly import _ASKER_TOKEN, asker_scope, set_backend_client

    BASE = "http://backend.test"
    route = respx.get(f"{BASE}/api/cycles/open").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "message": "ok",
                "data": [{"cycleId": 2, "cycleName": "2025 秋招"}],
            },
        )
    )
    settings = Settings(
        _env_file=None,
        backend_base_url=BASE,
        backend_service_username="svc",
        backend_service_password="secret",
    )
    client = BackendClient(http=httpx.AsyncClient(base_url=BASE), settings=settings)
    set_backend_client(client)
    try:
        seq = iter(
            [
                AIMessage("", tool_calls=[{"name": "get_open_cycle", "args": {}, "id": "call_1"}]),
                AIMessage("当前有 1 个开放周期:2025 秋招(cycleId=2)。"),
            ]
        )
        fake_model = _FakeToolCallingModel(messages=seq)

        import official_agent.graphs.assistant as assistant_mod

        # 强制 anthropic 假分支(同 react-loop 先例):本地 .env 若是
        # openai-compatible,不钉 provider 会构造真 ChatOpenAI(既慢又不定)。
        class _FakeSettings:
            llm_provider = "anthropic"
            model_strong = "fake"
            anthropic_api_key = ""
            llm_base_url = ""
            llm_api_key = ""

        monkeypatch.setattr(
            assistant_mod, "get_effective_settings", lambda: _FakeSettings(), raising=True
        )
        # 不 patch _ALL_TOOLS 的 get_open_cycle:保留真工具,让其经 _read 真实取数
        monkeypatch.setattr(
            assistant_mod, "ChatAnthropic", lambda **kwargs: fake_model, raising=True
        )
        agent = build_assistant_agent(identity_of("admin"))
        assert "get_open_cycle" in assistant_mod._ALL_TOOLS
        assert _ASKER_TOKEN.get() is None  # 无污染前提
        async with asker_scope("asker-jwt-xyz"):
            await agent.ainvoke({"messages": [HumanMessage("现在有开放周期吗?")]})
        assert _ASKER_TOKEN.get() is None  # 作用域结束已复位
        assert route.calls, "真 get_open_cycle 应经 _read 命中后端"
        assert route.calls[-1].request.headers.get("Authorization") == "Bearer asker-jwt-xyz"
    finally:
        await client.aclose()
        set_backend_client(None)


def test_role_tool_tables_stay_consistent() -> None:
    """防漂移:admin 档必须登记 _ALL_TOOLS 的全部工具(TOOL-05/SEC-02
    演化时,新工具只登记一张表会静默对全部角色不可见)。"""
    import official_agent.graphs.assistant as assistant_mod

    assert set(assistant_mod._ROLE_TOOL_NAMES["admin"]) == set(assistant_mod._ALL_TOOLS)
    for role, names in assistant_mod._ROLE_TOOL_NAMES.items():
        assert set(names) <= set(assistant_mod._ALL_TOOLS), f"{role} 档登记了未知工具"


def test_build_model_openai_compatible_branch() -> None:
    """provider=openai-compatible 构造 ChatOpenAI(base_url 指向 DeepSeek 等)。"""
    from langchain_openai import ChatOpenAI

    from official_agent.config import Settings
    from official_agent.graphs.assistant import build_model

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="openai-compatible",
        llm_base_url="https://api.deepseek.com",
        llm_api_key="sk-test",
        model_strong="deepseek-v4-flash",
    )
    model = build_model(settings)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "deepseek-v4-flash"


def test_build_model_openai_compatible_missing_config_fails() -> None:
    from official_agent.config import Settings
    from official_agent.graphs.assistant import build_model

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        llm_provider="openai-compatible",
        llm_base_url="",
        llm_api_key="",
    )
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        build_model(settings)


# ── 身份/工具契约进 system prompt(#166 修复)──────


def test_tool_contract_unknown_forbids_claims() -> None:
    """空工具档(unknown):明令不得声称查询——编造守卫第一层。"""
    from official_agent.graphs.assistant import tool_contract

    contract = tool_contract(identity_of("unknown"))
    assert "没有可用的数据查询工具" in contract
    assert "绝不能声称查询过" in contract
    assert "引导" in contract


def test_tool_contract_admin_lists_tools_and_failure_talk() -> None:
    from official_agent.graphs.assistant import tool_contract, tool_roster

    contract = tool_contract(identity_of("admin"))
    roster = tool_roster(identity_of("admin"))
    assert roster  # admin 有工具
    for name in roster:
        assert name in contract
    assert "不要声称「查询过」" in contract
    assert "不要编造结果" in contract


def test_identity_and_contract_live_in_system_prompt_not_user_message() -> None:
    """#166:身份/契约进 system prompt,绝不作为用户消息(泄漏成气泡)。"""
    from official_agent.graphs.assistant import (
        build_system_prompt,
        compose_first_message,
        load_system_prompt,
    )

    system = build_system_prompt(identity_of("admin"))
    # system 含静态正文 + 身份段 + 工具契约
    assert load_system_prompt() in system
    assert identity_message(identity_of("admin")) in system
    assert "数据查询工具:" in system
    # 首条用户消息不含任何内部字段(不再泄漏)
    first = compose_first_message("tok")
    assert "可访问权限" not in first
    assert "数据查询工具:" not in first


def test_tool_roster_matches_assembly() -> None:
    from official_agent.graphs.assistant import assemble_tools, tool_roster

    for role in ("admin", "member", "candidate", "unknown"):
        identity = identity_of(role)
        assert len(tool_roster(identity)) == len(assemble_tools(identity))
