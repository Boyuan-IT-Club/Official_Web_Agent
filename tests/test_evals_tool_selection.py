"""tool_selection executor 单测(#148):断言语义全 fake,不花 API 费。

真实图 × 真实 LLM 的链路由 test_assistant 覆盖;这里只验 runner 的断言面。
"""

from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from official_agent.evals import tool_selection


def _yaml(tmp_path: Path, case: dict[str, Any]) -> Path:
    import yaml

    path = tmp_path / "ts.yaml"
    path.write_text(yaml.dump([case], allow_unicode=True), encoding="utf-8")
    return path


def _fake_ainvoke(tool_calls: list[tuple[str, dict[str, Any]]]):
    """返回一个把工具调用序列原样吐回的 ainvoke fake(绕开真图)。"""

    async def ainvoke(agent: Any, input_text: str) -> dict[str, Any]:
        messages: list[Any] = [HumanMessage(input_text)]
        for i, (name, args) in enumerate(tool_calls):
            messages.append(
                AIMessage("", tool_calls=[{"name": name, "args": args, "id": f"call_{i}"}])
            )
            messages.append(ToolMessage("{}", tool_call_id=f"call_{i}", name=name))
        messages.append(AIMessage("done"))
        return {"messages": messages}

    return ainvoke


def _noop_factory(identity: Any, user_token: str) -> str:
    # 记录 user_token 传递(candidate 绑定路径)供断言
    _noop_factory.last_user_token = user_token  # type: ignore[attr-defined]
    return "fake-agent"


async def test_expected_and_params_subset_pass(tmp_path: Path) -> None:
    case = {
        "id": "ts-001",
        "role": "admin",
        "input": "技术部还有多少简历没筛?",
        "expected_tools": ["get_open_cycle", "search_resumes"],
        "expected_params": {"search_resumes": {"department": "技术部"}},
    }
    result = await tool_selection.run_suite(
        _yaml(tmp_path, case),
        build_agent=_noop_factory,
        ainvoke=_fake_ainvoke(
            [
                ("get_open_cycle", {}),
                ("search_resumes", {"cycleId": 2, "department": "技术部", "page": 1}),
            ]
        ),
    )
    assert result.status == "PASS"
    assert result.cases[0].passed


async def test_missing_expected_tool_fails(tmp_path: Path) -> None:
    case = {"id": "x", "role": "admin", "input": "q", "expected_tools": ["a", "b"]}
    result = await tool_selection.run_suite(
        _yaml(tmp_path, case),
        build_agent=_noop_factory,
        ainvoke=_fake_ainvoke([("a", {})]),
    )
    assert result.status == "FAIL"
    assert "未调用期望工具" in result.cases[0].detail


async def test_forbidden_tool_is_red_line(tmp_path: Path) -> None:
    case = {
        "id": "ts-003",
        "role": "candidate",
        "input": "q",
        "expected_tools": ["get_my_interview"],
        "forbidden_tools": ["search_resumes", "list_unassigned"],
    }
    result = await tool_selection.run_suite(
        _yaml(tmp_path, case),
        build_agent=_noop_factory,
        ainvoke=_fake_ainvoke([("get_my_interview", {}), ("search_resumes", {})]),
    )
    assert result.status == "FAIL"
    assert "禁止工具" in result.cases[0].detail
    # candidate 角色执行时携带用户令牌(绑定路径被走到)
    assert _noop_factory.last_user_token == "eval-user-token"  # type: ignore[attr-defined]


async def test_params_mismatch_fails(tmp_path: Path) -> None:
    case = {
        "id": "x",
        "role": "admin",
        "input": "q",
        "expected_tools": ["search_resumes"],
        "expected_params": {"search_resumes": {"department": "技术部"}},
    }
    result = await tool_selection.run_suite(
        _yaml(tmp_path, case),
        build_agent=_noop_factory,
        ainvoke=_fake_ainvoke([("search_resumes", {"department": "宣传部"})]),
    )
    assert result.status == "FAIL"
    assert "参数无一次匹配" in result.cases[0].detail


async def test_exact_mode_rejects_extra_calls(tmp_path: Path) -> None:
    case = {
        "id": "x",
        "role": "admin",
        "input": "q",
        "expected_tools": ["get_open_cycle"],
        "exact": True,
    }
    result = await tool_selection.run_suite(
        _yaml(tmp_path, case),
        build_agent=_noop_factory,
        ainvoke=_fake_ainvoke([("get_open_cycle", {}), ("search_resumes", {})]),
    )
    assert result.status == "FAIL"
    assert "exact" in result.cases[0].detail


async def test_executor_exception_becomes_failed_case_not_crash(tmp_path: Path) -> None:
    case = {"id": "x", "role": "admin", "input": "q", "expected_tools": ["a"]}

    async def boom(agent: Any, input_text: str) -> dict[str, Any]:
        raise RuntimeError("graph exploded")

    result = await tool_selection.run_suite(
        _yaml(tmp_path, case), build_agent=_noop_factory, ainvoke=boom
    )
    assert result.status == "FAIL"
    assert "执行异常" in result.cases[0].detail


async def test_fake_backend_routes_canned_responses() -> None:
    backend = tool_selection._FakeBackend()
    cycle = await backend.get("/api/cycles/open")
    assert cycle["cycleId"] == 2
    mine = await backend.get_as_user("/api/interview/schedule/my", user_token="t")
    assert "scheduleId" in mine
    sessions = await backend.get("/api/interview/admin/cycles/2/available-sessions")
    assert sessions[0]["sessionId"] == 5
    fallback = await backend.get("/api/unknown/path")
    assert fallback["evalFixture"] is True
    assert backend._token is not None  # 直填 token,不走登录
