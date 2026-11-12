"""组织记忆门禁执行器:真实助理(与产品同一装配)带着组织记忆回答问题。

断言两件事:候选人会话里组织记忆不出现;管理员与社员会话里相关的教训被用上。
数据集 evals/datasets/org_memory_probes.yaml。真实 LLM 驱动,无 key → SKIP;
后端 HTTP 由工具选择评测的 canned fake 顶替,不连数据库。

用例:
  role:    admin | member | candidate
  input:   用户的问题
  lessons: [{content, audience}]    组织记忆表里有的教训
  force_context: true               绕过召回那道闸,把全部教训硬塞进上下文——验装配层
                                    那道闸(候选人的助理没有组织记忆这一段)也守得住
  expect:
    answer_contains_any: [片段…]    回答里至少出现一个(教训被用上了)
    answer_not_contains: [片段…]    回答里一个都不能出现(没有泄漏、没有硬搬)

谁看得到哪些教训,走的是产品里同一个函数(memory.recall.org_memory_audiences),
只是把「从库里取」换成了「从用例里取」。
改组织记忆的召回、装配或 system prompt 里那一段说明,必须跑。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from official_agent.evals.engine import CaseResult, SuiteResult

AgentFactory = Callable[..., Any]
AgentRunner = Callable[[Any, str, list[str]], Awaitable[dict[str, Any]]]


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


def visible_lessons(identity: Any, lessons: list[dict[str, Any]]) -> list[str]:
    """这个身份召回得到的教训:按产品里的可见范围规则筛,条数按配置的上限截。

    评测要看的是「打开之后」的行为,所以在这里临时把开关拨开,用完还原。
    """
    from official_agent.config import get_settings
    from official_agent.memory import recall

    settings = get_settings()
    previous = settings.org_memory_enabled
    settings.org_memory_enabled = True
    try:
        audiences = recall.org_memory_audiences(identity)
    finally:
        settings.org_memory_enabled = previous
    limit = max(int(settings.org_memory_recall_limit), 1)
    visible = [lesson for lesson in lessons if lesson.get("audience", "admin") in audiences]
    return [str(lesson["content"]) for lesson in visible][:limit]


def final_answer(result: dict[str, Any]) -> str:
    """最后一条助手消息的文本。"""
    for message in reversed(result.get("messages", [])):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, list):
                content = "".join(
                    block.get("text", "") for block in content if isinstance(block, dict)
                )
            return str(content)
    return ""


def check_answer(answer: str, expect: dict[str, Any]) -> list[str]:
    """回答 vs 期望 → 不满足的条目(空表 = 通过)。"""
    problems: list[str] = []
    wanted = expect.get("answer_contains_any") or []
    if wanted and not any(fragment in answer for fragment in wanted):
        problems.append(f"回答里没有用上教训(应出现其一:{wanted})")
    for fragment in expect.get("answer_not_contains") or []:
        if fragment in answer:
            problems.append(f"回答里出现了不该出现的「{fragment}」")
    return problems


async def _default_ainvoke(agent: Any, input_text: str, org_memories: list[str]) -> dict[str, Any]:
    import secrets

    config = {"configurable": {"thread_id": f"eval:{secrets.token_hex(4)}"}}
    return await agent.ainvoke(
        {"messages": [HumanMessage(input_text)]},
        config,
        context={"memories": [], "org_memories": org_memories},
    )


async def run_suite(
    path: Path,
    *,
    build_agent: AgentFactory | None = None,
    ainvoke: AgentRunner | None = None,
    distribution: bool = False,
    **_: Any,
) -> SuiteResult:
    """逐用例构造助理并提问。build_agent / ainvoke 可注入假实现(单测不花 API 费)。"""
    from official_agent.evals import tool_selection
    from official_agent.tools import readonly

    build_agent = build_agent or tool_selection._default_factory
    ainvoke = ainvoke or _default_ainvoke
    data = _load_yaml(path) or {}
    min_pass_rate = float((data.get("defaults") or {}).get("min_pass_rate", 1.0))
    cases: list[CaseResult] = []
    for case in data.get("cases") or []:
        role = str(case["role"])
        lessons = list(case.get("lessons") or [])
        readonly.set_backend_client(tool_selection._FakeBackend())
        try:
            identity = tool_selection._identity_of(role)
            given = (
                [str(lesson["content"]) for lesson in lessons]
                if case.get("force_context")
                else visible_lessons(identity, lessons)
            )
            agent = build_agent(identity, "eval-user-token" if role == "candidate" else "")
            result = await ainvoke(agent, str(case["input"]), given)
            problems = check_answer(final_answer(result), case.get("expect") or {})
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            problems = [f"{type(exc).__name__}: {exc}"[:160]]
        finally:
            readonly.set_backend_client(None)
        cases.append(CaseResult(id=str(case["id"]), passed=not problems, detail=";".join(problems)))

    passed = sum(1 for c in cases if c.passed)
    rate = passed / len(cases) if cases else 1.0
    return SuiteResult(
        name=path.stem,
        kind="org_memory_probes",
        source=path.name,
        status="PASS" if rate >= min_pass_rate else "FAIL",
        cases=cases,
        metrics={"org_memory_pass_rate": round(rate, 3)},
        notes=[f"通过 {passed}/{len(cases)},门槛 {min_pass_rate}"],
    )
