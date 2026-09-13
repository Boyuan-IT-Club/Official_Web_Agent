"""工具选择用例 executor(OBS-03,#148 收口)。

吃 cases/*.yaml:role + input → 驱动真实 assistant ReAct 图(真实 LLM),
后端 HTTP 用 canned fake 顶替(评测只关心「调了哪些工具、参数对不对」,
不关心后端真数据)。断言语义:

- ``expected_tools`` :每个都必须被调用(集合语义,顺序不敏感)
- ``forbidden_tools``:任一被调用即 FAIL(权限红线,如候选人触管理工具)
- ``expected_params``:该工具至少一次调用的参数包含全部期望键值(子集匹配)
- ``exact: true``    :调用集合必须与 expected_tools 完全相等(默认不要求)

确定性边界:LLM 非零温,断言用集合/子集而非轨迹全文以抗抖动;个别 case
仍可复现性差时应在 case 上加约束或改写输入,不在 runner 里加随机重试。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import yaml
from langchain_core.messages import AIMessage, HumanMessage

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.tools import readonly
from official_agent.tools.client import BackendClient

#: build_agent 注入形状:(identity, user_token) → 可 ainvoke 的图(同步工厂)
AgentFactory = Callable[[Any, str], Any]
AgentRunner = Callable[[Any, str], Awaitable[Any]]


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


def _default_factory(identity: Any, user_token: str) -> Any:
    """真实装配路径:与产品同一构建函数,仅后端 HTTP 被 fake 顶替。

    非流式 ainvoke,stream_usage 必须 False(openai-compatible 端点约束)。"""
    from official_agent.graphs.assistant import build_assistant_agent

    return build_assistant_agent(identity, user_token=user_token, stream_usage=False)


class _FakeBackend(BackendClient):
    """按路径路由 canned 响应的 BackendClient 顶替(只实现工具会打的入口)。

    绕过父类 __init__(不建 httpx 客户端/不登录,token 直填);工具层只经
    get/get_as_user 取数。响应为后端原始结构(工具层再做投影裁剪,评测
    透传即可)。
    """

    def __init__(self) -> None:
        self._token: str | None = "eval-service-token"
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def _route(self, path: str, params: dict[str, Any] | None) -> Any:
        self.calls.append((path, params))
        if path == "/api/cycles/open":
            return {"cycleId": 2, "cycleName": "2025 秋招", "isActive": True}
        if path == "/api/resumes/search":
            return {
                "content": [
                    {"resumeId": 11, "name": "张三", "department": "技术部", "status": "PENDING"},
                    {"resumeId": 12, "name": "李四", "department": "技术部", "status": "PENDING"},
                ],
                "totalElements": 2,
            }
        if path.endswith("/available-sessions"):
            return [
                {"sessionId": 5, "date": "2026-09-11", "startTime": "14:00", "location": "线上"},
            ]
        if path == "/api/interview/schedule/my":
            return {"scheduleId": 9, "interviewTime": "2026-09-11 14:00", "location": "线上"}
        return {"evalFixture": True, "path": path}

    async def get(
        self, path: str, params: dict[str, Any] | None = None, headers: Any = None
    ) -> Any:
        return self._route(path, params)

    async def get_as_user(
        self, path: str, params: dict[str, Any] | None = None, user_token: str = ""
    ) -> Any:
        return self._route(path, params)


def _identity_of(role: str) -> ResolvedIdentity:
    """评测身份:与 test_assistant 同构,空权限面(权限差异由 forbidden 断言兜)。"""
    return cast(
        ResolvedIdentity,
        {
            "user_id": 7,
            "name": "评测用户",
            "role": role,
            "role_names": [role],
            "permission_codes": [],
            "source": "eval",
        },
    )


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


async def run_suite(
    path: Path,
    *,
    build_agent: AgentFactory | None = None,
    ainvoke: AgentRunner | None = None,
    distribution: bool = False,
) -> SuiteResult:
    """逐 case 构图执行。build_agent/ainvoke 可注入 fake(单测不花 API 费)。"""
    if build_agent is None:
        build_agent = _default_factory
    if ainvoke is None:
        ainvoke = _default_ainvoke

    data = _load_yaml(path)
    # 显式格式 {runner, cases:[...]};裸列表为早期格式兼容
    cases_yaml = data["cases"] if isinstance(data, dict) else data
    results: list[CaseResult] = []
    for case in cases_yaml:
        role = str(case["role"])
        user_token = "eval-user-token" if role == "candidate" else ""
        backend = _FakeBackend()
        readonly.set_backend_client(backend)
        try:
            agent = build_agent(_identity_of(role), user_token)
            result = await ainvoke(agent, str(case["input"]))
            calls = _extract_tool_calls(result.get("messages", []))
            passed, detail = _case_passed(case, calls)
        except Exception as exc:  # noqa: BLE001 - 单 case 炸不拖垮整 suite
            passed, detail = False, f"执行异常: {type(exc).__name__}: {exc}"
        finally:
            readonly.set_backend_client(None)
        results.append(CaseResult(id=str(case["id"]), passed=passed, detail=detail))

    return SuiteResult(
        name=path.stem,
        kind="tool_selection",
        source=path.name,
        status="FAIL" if any(not c.passed for c in results) else "PASS",
        cases=results,
    )


async def _default_ainvoke(agent: Any, input_text: str) -> dict[str, Any]:
    return await agent.ainvoke({"messages": [HumanMessage(input_text)]})
