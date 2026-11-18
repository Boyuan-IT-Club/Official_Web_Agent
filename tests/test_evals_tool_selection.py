"""tool_selection executor 单测:断言语义全 fake,不花 API 费。

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

    async def ainvoke(agent: Any, input_text: str, **_: Any) -> dict[str, Any]:
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

    async def boom(agent: Any, input_text: str, **_: Any) -> dict[str, Any]:
        raise RuntimeError("graph exploded")

    result = await tool_selection.run_suite(
        _yaml(tmp_path, case), build_agent=_noop_factory, ainvoke=boom
    )
    assert result.status == "FAIL"
    assert "执行异常" in result.cases[0].detail


async def test_fake_backend_routes_canned_responses() -> None:
    backend = tool_selection._FakeBackend()
    cycle = await backend.get("/api/cycles/open")
    assert cycle[0]["cycleId"] == 2
    mine = await backend.get_as_user("/api/interview/schedule/my", user_token="t")
    assert "scheduleId" in mine
    sessions = await backend.get("/api/interview/admin/cycles/2/available-sessions")
    assert sessions[0]["sessionId"] == 5
    fallback = await backend.get("/api/unknown/path")
    assert fallback["evalFixture"] is True
    assert backend._token is not None  # 直填 token,不走登录


# ── 多轮与意图提示 ─────────────────────────────────────


def _session_listing() -> str:
    """上一轮 find_available_sessions 的工具消息内容(与装配层包装后的形状一致)。"""
    from official_agent.security.injection_guard import guard_tool_result

    data = [{"sessionId": 5 + i, "date": "2026-09-11", "start": f"1{4 + i}:00"} for i in range(3)]
    wrapped, _ = guard_tool_result("find_available_sessions", {"status": "ok", "data": data})
    return wrapped


class _ThreadedFake:
    """按 thread 累积消息的假图:记录每轮收到的 context,首轮列三个场次,末轮调写工具。"""

    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []
        self.threads: set[str] = set()
        self.history: list[Any] = []

    async def __call__(self, agent: Any, text: str, *, config: Any, context: Any) -> dict:
        self.contexts.append(context)
        self.threads.add(config["configurable"]["thread_id"])
        self.history.append(HumanMessage(text))
        if len(self.contexts) == 1:
            call = {"name": "find_available_sessions", "args": {"cycle_id": 2}, "id": "c0"}
            self.history += [
                AIMessage("", tool_calls=[call]),
                ToolMessage(_session_listing(), tool_call_id="c0", name="find_available_sessions"),
                AIMessage("有三个场次"),
            ]
        else:
            call = {
                "name": "assign_interview",
                "args": {"resume_id": 11, "target_session_id": 6},
                "id": "c1",
            }
            self.history.append(
                AIMessage("", tool_calls=[call], usage_metadata={
                    "input_tokens": 90, "output_tokens": 10, "total_tokens": 100
                })
            )
        return {"messages": list(self.history)}


def _multi_turn_case() -> dict[str, Any]:
    return {
        "id": "pick",
        "role": "admin",
        "allow_write": True,
        "permission_codes": ["interview:schedule"],
        "history": ["9 月 11 日下午还有哪些场次?"],
        "input": "把简历 11 排到第二个",
        "expected_tools": ["assign_interview"],
        "expected_params": {"assign_interview": {"target_session_id": 6}},
        "forbidden_tools": ["find_available_sessions"],  # 只断言最后一轮:首轮的查询不算
    }


async def test_history_runs_on_one_thread_and_asserts_only_last_turn(tmp_path: Path) -> None:
    fake = _ThreadedFake()
    built: dict[str, Any] = {}

    def factory(identity: Any, user_token: str, **kwargs: Any) -> str:
        built.update(kwargs)
        return "fake-agent"

    result = await tool_selection.run_suite(
        _yaml(tmp_path, _multi_turn_case()), build_agent=factory, ainvoke=fake, intent_mode="off"
    )
    assert result.status == "PASS", result.cases[0].detail
    assert built == {"allow_write": True, "multi_turn": True}  # 多轮要 checkpointer
    assert len(fake.threads) == 1 and len(fake.contexts) == 2
    assert fake.contexts == [{}, {}]  # off 档不给图任何意图上下文
    assert result.context["intent_mode"] == "off"
    assert result.observations["model_calls_mean"] == 1
    assert result.observations["tokens_mean"] == 100


async def test_hint_mode_feeds_per_turn_hint_built_from_prior_turn(tmp_path: Path) -> None:
    fake = _ThreadedFake()
    result = await tool_selection.run_suite(
        _yaml(tmp_path, _multi_turn_case()),
        build_agent=lambda *a, **k: "fake-agent",
        ainvoke=fake,
        intent_mode="hint",
    )
    assert result.status == "PASS", result.cases[0].detail
    last_hint = fake.contexts[-1]["intent_hint"]
    assert "第 2 项" in last_hint and "场次 #6" in last_hint  # 从上一轮结果解析出了指代
    assert "assign_interview" in last_hint
    assert result.cases[0].detail.startswith("[intent=schedule_interview]")
    assert result.context["intent_version"] and result.context["hint_prompt"]


async def test_shadow_mode_labels_cases_without_hinting(tmp_path: Path) -> None:
    fake = _ThreadedFake()
    result = await tool_selection.run_suite(
        _yaml(tmp_path, _multi_turn_case()),
        build_agent=lambda *a, **k: "fake-agent",
        ainvoke=fake,
        intent_mode="shadow",
    )
    assert all(ctx == {"intent_hint": ""} for ctx in fake.contexts)
    assert result.cases[0].detail.startswith("[intent=")


async def test_fake_backend_session_override_and_reschedule_list() -> None:
    backend = tool_selection._FakeBackend(sessions=[{"sessionId": 9}])
    assert await backend.get("/api/interview/admin/cycles/2/available-sessions") == [
        {"sessionId": 9}
    ]
    requests = await backend.get("/api/interview/reschedule/admin/list", {"cycleId": 2})
    assert requests[0]["requestId"] == 27


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 21)]
    assert tool_selection._percentile(values, 50) == 10
    assert tool_selection._percentile(values, 95) == 19
    assert tool_selection._percentile([7.0], 95) == 7
