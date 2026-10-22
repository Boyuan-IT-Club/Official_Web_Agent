"""长期记忆提取门禁执行器:合成对话跑「提示词 → 模型 → 解析 → 确定性闸门」,不落库。

数据集 evals/datasets/memory_probes.yaml。真实 LLM 驱动,无 key → SKIP。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


def check_ops(ops: Any, expect: dict[str, Any]) -> list[str]:
    """过闸门后的提取结果 vs 期望 → 不满足的条目(空表 = 通过)。"""
    problems: list[str] = []
    added = [a.content for a in ops.add]
    if expect.get("expect_empty") and (ops.add or ops.update or ops.delete):
        problems.append(f"应不记忆,实际 add={added} update={len(ops.update)} delete={ops.delete}")
    for fragment in expect.get("add_contains") or []:
        if not any(fragment in a for a in added):
            problems.append(f"新增记忆缺「{fragment}」:{added}")
    if "update_ids" in expect:
        got = sorted(u.memory_id for u in ops.update)
        if got != sorted(expect["update_ids"]):
            problems.append(f"改写 id={got} 期望 {expect['update_ids']}")
    return problems


async def run_suite(
    path: Path, *, model: Any = None, distribution: bool = False, **_: Any
) -> SuiteResult:
    from official_agent.memory import extract, store

    if model is None:
        from official_agent.config import get_effective_settings
        from official_agent.graphs.assistant import build_model

        model = build_model(get_effective_settings()).bind(temperature=0)

    data = _load_yaml(path) or {}
    min_pass_rate = float((data.get("defaults") or {}).get("min_pass_rate", 1.0))
    cases: list[CaseResult] = []
    for case in data.get("cases") or []:
        existing = [
            store.Memory(
                memory_id=str(m["memory_id"]),
                owner_user_id=0,
                content=str(m["content"]),
                kind=str(m.get("kind", "fact")),
                source_thread="",
            )
            for m in case.get("existing") or []
        ]
        try:
            response = await model.ainvoke(extract.build_prompt(existing, list(case["said"])))
            raw = response.content
            content = raw if isinstance(raw, str) else str(raw)
            ops = extract.screen(extract._parse(content), existing)
            problems = check_ops(ops, case.get("expect") or {})
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            problems = [f"{type(exc).__name__}: {exc}"[:160]]
        cases.append(CaseResult(id=case["id"], passed=not problems, detail=";".join(problems)))

    passed = sum(1 for c in cases if c.passed)
    rate = passed / len(cases) if cases else 1.0
    return SuiteResult(
        name=path.stem,
        kind="memory_probes",
        source=path.name,
        status="PASS" if rate >= min_pass_rate else "FAIL",
        cases=cases,
        metrics={"memory_pass_rate": round(rate, 3)},
        notes=[f"通过 {passed}/{len(cases)},门槛 {min_pass_rate}"],
    )
