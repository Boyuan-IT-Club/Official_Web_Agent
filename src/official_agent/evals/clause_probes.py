"""评分条款归纳探针:给定人机看法不一致的判定项与样例,看归纳出来的候选站不站得住。

数据集 evals/datasets/clause_probes.yaml。真实 LLM 驱动,无 key → SKIP。
每条用例给一组待归纳的条目(形状同 panel.clauses.aggregate_disagreements 的输出),
跑线上同一条归纳路径(同一份 prompt、同一套过闸),再核对:
- min_candidates:至少归纳出几条过了闸的候选;
- max_rejected:  被闸掉的候选至多几条(模型照抄了个人信息或指令样文本就会被闸掉);
- lacks:         候选文字里不该出现的片段。
指标 clause_pass_rate 是通过的用例占比,低于 defaults.min_pass_rate → FAIL。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult

#: 归纳入口的形状(测试注入假实现):待归纳的条目 → (过了闸的候选, 被闸掉的条数)
InduceFn = Callable[[list[dict[str, Any]]], Awaitable[tuple[list[dict[str, Any]], int]]]


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


def groups_of(case: dict[str, Any]) -> list[dict[str, Any]]:
    """用例里的条目 → 归纳入口要的形状;清单、判定项或方向写错直接抛。"""
    from official_agent.evals.panel_cases import item_key

    case_id = str(case.get("id", "?"))
    groups = []
    for raw in case.get("groups") or []:
        checklist, item = item_key(raw, case_id=case_id)
        if raw.get("direction") not in ("lenient", "strict"):
            raise ValueError(f"用例 {case_id}:direction 只能是 lenient 或 strict")
        samples = [
            {"quote": str(s.get("quote", "")), "reason": str(s.get("reason", ""))}
            for s in raw.get("samples") or []
        ]
        groups.append(
            {
                "checklist": checklist,
                "item": item,
                "direction": raw["direction"],
                "cards": int(raw["cards"]),
                "samples": samples,
            }
        )
    return groups


def check_candidates(
    accepted: list[dict[str, Any]], rejected: int, expect: dict[str, Any]
) -> list[str]:
    """归纳结果 vs 期望 → 不符的条目(空表 = 通过)。纯函数。"""
    problems: list[str] = []
    if len(accepted) < int(expect.get("min_candidates", 0)):
        problems.append(f"只归纳出 {len(accepted)} 条候选,期望至少 {expect['min_candidates']} 条")
    if "max_rejected" in expect and rejected > int(expect["max_rejected"]):
        problems.append(f"{rejected} 条候选被闸掉,期望至多 {expect['max_rejected']} 条")
    text = "\n".join(candidate["clause"] for candidate in accepted)
    problems += [f"候选文字里出现了「{raw}」" for raw in expect.get("lacks") or [] if raw in text]
    return problems


async def run_suite(
    path: Path, *, induce: InduceFn | None = None, distribution: bool = False, **_: Any
) -> SuiteResult:
    if induce is None:
        from official_agent.evaluation.clause_candidates import induce as _induce

        induce = _induce

    data = _load_yaml(path) or {}
    min_pass_rate = float((data.get("defaults") or {}).get("min_pass_rate", 0.75))
    cases: list[CaseResult] = []
    for case in data.get("cases") or []:
        groups = groups_of(case)
        try:
            accepted, rejected = await induce(groups)
            problems = check_candidates(accepted, rejected, case.get("expect") or {})
            detail = f"{len(accepted)} 条候选,{rejected} 条被闸掉"
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            problems, detail = [f"{type(exc).__name__}: {exc}"[:160]], ""
        cases.append(
            CaseResult(
                id=str(case["id"]),
                passed=not problems,
                detail=";".join(part for part in [detail, *problems] if part),
            )
        )

    passed = sum(1 for c in cases if c.passed)
    rate = passed / len(cases) if cases else 1.0
    return SuiteResult(
        name=path.stem,
        kind="clause_probes",
        source=path.name,
        status="PASS" if rate >= min_pass_rate else "FAIL",
        cases=cases,
        metrics={"clause_pass_rate": round(rate, 3)},
        notes=[f"通过 {passed}/{len(cases)},门槛 {min_pass_rate}"],
    )
