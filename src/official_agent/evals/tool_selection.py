"""工具选择用例 executor。

吃 cases/*.yaml:role + input → 驱动真实 assistant ReAct 图(真实 LLM),
后端 HTTP 用 canned fake 顶替(评测只关心「调了哪些工具、参数对不对」,
不关心后端真数据)。断言语义:

- ``expected_tools`` :每个都必须被调用(集合语义,顺序不敏感)
- ``forbidden_tools``:任一被调用即 FAIL(权限红线,如候选人触管理工具)
- ``expected_params``:该工具至少一次调用的参数包含全部期望键值(子集匹配)
- ``exact: true``    :调用集合必须与 expected_tools 完全相等(默认不要求)
- ``allow_write`` / ``permission_codes``:写工具用例——按官网通道装配写工具,
  运行停在确认挂起处,写操作不会执行
- ``history``:先在同一会话里依次发这些消息,断言只看最后一条 ``input`` 那一轮的调用
  (多轮指代,如上一轮列场次、这一轮说「第二个」);history 里不要放写请求——停在
  确认处的会话再发新消息,需要 web 入口的取消逻辑,这里不模拟
- ``sessions``:覆盖假后端的可用场次(缺省一个场次 #5)

意图提示:``run_suite(intent_mode=...)`` 缺省取 config.assistant_intent_mode(命令行
``--intent-mode`` 覆盖)。shadow/hint 档逐轮按 web 入口同一套函数算意图,hint 档把提示
经运行时上下文交给图;同一份用例分别以 off 与 hint 跑两次,即是对照评测。
每条用例的明细带上意图标签;observations 记最后一轮的耗时分位、模型调用数与 token,
不设门(方向不是「越高越好」)。

确定性边界:LLM 非零温,断言用集合/子集而非轨迹全文以抗抖动;个别 case
仍可复现性差时应在 case 上加约束或改写输入,不在 runner 里加随机重试。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import yaml
from langchain_core.messages import AIMessage, HumanMessage

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.tools import readonly
from official_agent.tools.client import BackendClient

#: build_agent 注入形状:(identity, user_token, **装配选项) → 可 ainvoke 的图(同步工厂)
AgentFactory = Callable[..., Any]
#: ainvoke 注入形状:(agent, 本轮文本, *, config, context) → 图的最终状态
AgentRunner = Callable[..., Awaitable[Any]]


def env_blocker() -> str | None:
    """真实 LLM 驱动,无 key 则 SKIP。"""
    from official_agent.config import get_settings

    settings = get_settings()
    if settings.llm_provider == "openai-compatible":
        if not settings.llm_api_key or not settings.llm_base_url:
            return "LLM_BASE_URL/LLM_API_KEY 未配置(工具选择用例需要真实 LLM)"
        return None
    if not settings.anthropic_api_key:
        return "ANTHROPIC_API_KEY 未配置(工具选择用例需要真实 LLM)"
    return None


def _default_factory(
    identity: Any, user_token: str, *, allow_write: bool = False, multi_turn: bool = False
) -> Any:
    """真实装配路径:与产品同一构建函数,仅后端 HTTP 被 fake 顶替。

    非流式 ainvoke,stream_usage 必须 False(openai-compatible 端点约束)。
    allow_write:写工具用例。配内存 checkpointer 让写工具能挂起——运行停在
    确认处,写操作永远不会真正执行,评测只看模型有没有选对工具、参数对不对。
    multi_turn:带 history 的用例,同样要内存 checkpointer 才能跨轮记住上下文。"""
    from official_agent.graphs.assistant import build_assistant_agent

    if not allow_write and not multi_turn:
        return build_assistant_agent(identity, user_token=user_token, stream_usage=False)
    from langgraph.checkpoint.memory import MemorySaver

    return build_assistant_agent(
        identity,
        user_token=user_token,
        stream_usage=False,
        checkpointer=MemorySaver(),
        allow_write=allow_write,
    )


class _FakeBackend(BackendClient):
    """按路径路由 canned 响应的 BackendClient 顶替(只实现工具会打的入口)。

    绕过父类 __init__(不建 httpx 客户端/不登录,token 直填);工具层只经
    get/get_as_user 取数。响应为后端原始结构(工具层再做投影裁剪,评测
    透传即可)。
    """

    def __init__(self, sessions: list[dict[str, Any]] | None = None) -> None:
        self._token: str | None = "eval-service-token"
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._sessions = sessions

    def _route(self, path: str, params: dict[str, Any] | None) -> Any:
        self.calls.append((path, params))
        if path == "/api/cycles/open":
            return [
                {
                    "cycleId": 2,
                    "cycleName": "2026 秋季招新",
                    "academicYear": "2026-2027",
                    "intakeOpen": True,
                }
            ]
        if path == "/api/resumes/search":
            return {
                "content": [
                    {"resumeId": 11, "name": "张三", "department": "技术部", "status": "PENDING"},
                    {"resumeId": 12, "name": "李四", "department": "技术部", "status": "PENDING"},
                ],
                "totalElements": 2,
            }
        if path.endswith("/available-sessions"):
            if self._sessions is not None:
                return self._sessions
            # 字段与后端 InterviewSessionDTO 一致(工具层按真实字段投影)
            return [
                {
                    "sessionId": 5,
                    "interviewDate": "2026-09-11",
                    "startTime": "14:00",
                    "endTime": "16:00",
                    "deptName": "技术部",
                    "location": "线上",
                    "capacity": 8,
                    "remaining": 3,
                },
            ]
        if path == "/api/interview/schedule/my":
            return {"scheduleId": 9, "interviewTime": "2026-09-11 14:00", "location": "线上"}
        if path == "/api/interview/reschedule/admin/list":
            return [
                {
                    "requestId": 27,
                    "requestType": "RESCHEDULE",
                    "reason": "与考试时间冲突",
                    "currentInterviewTime": "2026-09-11 14:00",
                    "currentLocation": "线上",
                    "preferredSlots": [{"label": "9 月 13 日上午"}],
                    "submittedAt": "2026-09-08 20:00",
                    "status": 0,
                    "awaitingReassign": False,
                }
            ]
        return {"evalFixture": True, "path": path}

    async def get(
        self, path: str, params: dict[str, Any] | None = None, headers: Any = None
    ) -> Any:
        return self._route(path, params)

    async def get_as_user(
        self, path: str, params: dict[str, Any] | None = None, user_token: str = ""
    ) -> Any:
        return self._route(path, params)


def _identity_of(role: str, permission_codes: list[str] | None = None) -> ResolvedIdentity:
    """评测身份:与 test_assistant 同构;权限码缺省为空(权限差异由 forbidden 断言兜)。"""
    return cast(
        ResolvedIdentity,
        {
            "user_id": 7,
            "name": "评测用户",
            "role": role,
            "role_names": [role],
            "permission_codes": list(permission_codes or []),
            "source": "eval",
        },
    )


def _current_turn(messages: list[Any]) -> list[Any]:
    """最后一条用户消息之后的消息,即被断言的那一轮。"""
    last_human = max(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1
    )
    return messages[last_human + 1 :]


def _extract_tool_calls(messages: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    """按序抽出全部工具调用 (name, args)。"""
    calls: list[tuple[str, dict[str, Any]]] = []
    for msg in messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                calls.append((tc["name"], dict(tc.get("args") or {})))
    return calls


def _case_passed(case: dict[str, Any], calls: list[tuple[str, dict[str, Any]]]) -> tuple[bool, str]:
    names = [n for n, _ in calls]
    name_set = set(names)

    expected = set(case.get("expected_tools") or [])
    missing = expected - name_set
    if missing:
        return False, f"未调用期望工具: {sorted(missing)};实际调用 {names}"

    forbidden = set(case.get("forbidden_tools") or [])
    hit_forbidden = forbidden & name_set
    if hit_forbidden:
        return False, f"调用了禁止工具: {sorted(hit_forbidden)}(权限红线);实际调用 {names}"

    if case.get("exact") and name_set != expected:
        extra = name_set - expected
        return False, f"exact 模式调用集合不符,多调: {sorted(extra)};实际调用 {names}"

    for tool, want in (case.get("expected_params") or {}).items():
        if tool not in name_set:
            return False, f"参数断言目标 {tool} 未被调用"
        args_list = [args for n, args in calls if n == tool]
        if not any(all(args.get(k) == v for k, v in want.items()) for args in args_list):
            return False, f"{tool} 参数无一次匹配 {want};实际 {args_list}"

    return True, f"调用 {names}"


def _load_yaml(path: Path) -> Any:
    """同步读盘抽小函数(ASYNC240:异步体内不做阻塞 IO)。"""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _turn_context(
    text: str, prior: list[Any], *, mode: str, tool_names: list[str]
) -> tuple[dict[str, Any], str | None]:
    """按 web 入口同一套函数算本轮意图 → (运行时上下文, 意图标签);off 档标签为 None。"""
    if mode == "off":
        return {}, None
    from official_agent.graphs.assistant import intent as turn_intent

    result = turn_intent.classify_turn(
        text, prior_options=turn_intent.prior_options_from_messages(prior)
    )
    hint = turn_intent.render_hint(result, tool_names) if mode == "hint" else ""
    return {"intent_hint": hint}, result.label


def _turn_cost(messages: list[Any]) -> tuple[int, int | None]:
    """一轮的 (模型调用数, 总 token);没有任何 usage 时 token 为 None。"""
    replies = [m for m in messages if isinstance(m, AIMessage)]
    usages = [m.usage_metadata for m in replies if getattr(m, "usage_metadata", None)]
    tokens = sum(int(u.get("total_tokens") or 0) for u in usages) if usages else None
    return len(replies), tokens


def _percentile(values: list[float], q: float) -> float:
    """最近秩分位数:小样本下不插值,报出来的就是某一条真实耗时。"""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil(n*q/100)
    return ordered[int(rank) - 1]


def _observations(latencies: list[float], calls: list[int], tokens: list[int]) -> dict:
    if not latencies:
        return {}
    observed = {
        "latency_p50_ms": round(_percentile(latencies, 50)),
        "latency_p95_ms": round(_percentile(latencies, 95)),
        "model_calls_mean": round(sum(calls) / len(calls), 2),
    }
    if tokens:
        observed["tokens_mean"] = round(sum(tokens) / len(tokens), 1)
    return observed


async def run_suite(
    path: Path,
    *,
    build_agent: AgentFactory | None = None,
    ainvoke: AgentRunner | None = None,
    distribution: bool = False,
    intent_mode: str | None = None,
) -> SuiteResult:
    """逐 case 构图执行。build_agent/ainvoke 可注入 fake(单测不花 API 费)。"""
    from official_agent.config import get_settings
    from official_agent.graphs.assistant import intent as turn_intent
    from official_agent.graphs.assistant import tool_roster

    if build_agent is None:
        build_agent = _default_factory
    if ainvoke is None:
        ainvoke = _default_ainvoke
    settings = get_settings()
    mode = intent_mode or settings.assistant_intent_mode

    data = _load_yaml(path)
    # 显式格式 {runner, cases:[...]};裸列表为早期格式兼容
    cases_yaml = data["cases"] if isinstance(data, dict) else data
    results: list[CaseResult] = []
    latencies: list[float] = []
    model_calls: list[int] = []
    tokens: list[int] = []
    for case in cases_yaml:
        role = str(case["role"])
        user_token = "eval-user-token" if role == "candidate" else ""
        backend = _FakeBackend(sessions=case.get("sessions"))
        readonly.set_backend_client(backend)
        history = [str(h) for h in case.get("history") or []]
        try:
            identity = _identity_of(role, case.get("permission_codes"))
            # 用例显式声明才走写装配/多轮(注入的假工厂不受影响)
            build_kwargs: dict[str, Any] = {}
            if case.get("allow_write"):
                build_kwargs["allow_write"] = True
            if history:
                build_kwargs["multi_turn"] = True
            agent = build_agent(identity, user_token, **build_kwargs)
            tool_names = tool_roster(identity, allow_write=bool(case.get("allow_write")))
            config = {"configurable": {"thread_id": f"eval:{secrets.token_hex(4)}"}}
            messages: list[Any] = []
            label: str | None = None
            elapsed_ms = 0.0
            for text in [*history, str(case["input"])]:
                context, label = _turn_context(text, messages, mode=mode, tool_names=tool_names)
                started = time.monotonic()
                result = await ainvoke(agent, text, config=config, context=context)
                elapsed_ms = (time.monotonic() - started) * 1000
                messages = list(result.get("messages", []))
            turn = _current_turn(messages)
            calls = _extract_tool_calls(turn)
            passed, detail = _case_passed(case, calls)
            if label is not None:
                detail = f"[intent={label}] {detail}"
            latencies.append(elapsed_ms)
            n_calls, n_tokens = _turn_cost(turn)
            model_calls.append(n_calls)
            if n_tokens is not None:
                tokens.append(n_tokens)
        except Exception as exc:  # noqa: BLE001 - 单 case 炸不拖垮整 suite
            passed, detail = False, f"执行异常: {type(exc).__name__}: {exc}"
        finally:
            readonly.set_backend_client(None)
        results.append(CaseResult(id=str(case["id"]), passed=passed, detail=detail))

    context: dict[str, Any] = {"intent_mode": mode, "model": settings.model_strong}
    if mode != "off":
        context["intent_version"] = turn_intent.INTENT_VERSION
    if mode == "hint":
        context["hint_prompt"] = turn_intent.hint_prompt_version()
    return SuiteResult(
        name=path.stem,
        kind="tool_selection",
        source=path.name,
        status="FAIL" if any(not c.passed for c in results) else "PASS",
        cases=results,
        observations=_observations(latencies, model_calls, tokens),
        context=context,
    )


async def _default_ainvoke(
    agent: Any, input_text: str, *, config: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    # off 档不传上下文:与意图提示上线前的调用形状一致,基线才可比
    return await agent.ainvoke(
        {"messages": [HumanMessage(input_text)]}, config, context=context or None
    )
