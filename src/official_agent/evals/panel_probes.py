"""合议汇总规则探针:给定评委判定、批判者质疑与答辩重判,核对代码汇总出的结论。

数据集 evals/datasets/panel_probes.yaml。纯确定性(只跑 panel.aggregate.settle),
不调模型、无环境依赖,任何一条不符即 FAIL。这些规则决定哪些达成项作废、哪些卡进
低置信队列,改动必须过这里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evals.panel_cases import item_key, judgement_of


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None  # 纯代码规则,无环境依赖


def _critique_of(case: dict[str, Any]) -> dict[str, Any] | None:
    """用例里的 critic 段 → 批判者质疑。写 unavailable 表示批判者不可用;省略表示没有意见。"""
    raw = case.get("critic")
    if raw == "unavailable":
        return None
    raw = raw or {}
    case_id = str(case.get("id", "?"))
    challenges = []
    for ch in raw.get("challenges") or []:
        checklist, item = item_key(ch, case_id=case_id)
        challenges.append(
            {
                "checklist": checklist,
                "item": item,
                "kind": str(ch.get("kind", "quote_not_supporting")),
                "quote": str(ch.get("quote", "")),
                "reason": str(ch.get("reason", "数据集给定的质疑")),
            }
        )
    return {
        "injection_spans": [str(s) for s in raw.get("injection_spans") or []],
        "challenges": challenges,
        "agrees_bad_faith": raw.get("agrees_bad_faith"),
        "dropped": 0,
    }


def _rebuttal_of(case: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]] | None:
    """用例里的 rebuttal 段 → 评委的重判;省略表示没有答辩(没有质疑,或答辩失败)。"""
    raw = case.get("rebuttal")
    if raw is None:
        return None
    case_id = str(case.get("id", "?"))
    return {
        item_key(v, case_id=case_id): {
            "item": str(v["item"]),
            "met": bool(v.get("met")),
            "quote": str(v.get("quote", "")),
            "reason": str(v.get("reason", "数据集给定的重判")),
        }
        for v in raw
    }


def check_settlement(settlement: Any, expect: dict[str, Any], *, case_id: str) -> list[str]:
    """汇总结果 vs 期望 → 不符的条目(空表 = 通过)。"""
    panel = settlement.panel
    problems: list[str] = []
    if "confidence" in expect and panel["confidence"] != expect["confidence"]:
        problems.append(f"置信度={panel['confidence']} 期望 {expect['confidence']}")
    if "hard_zero" in expect and settlement.hard_zero != bool(expect["hard_zero"]):
        problems.append(f"hard_zero={settlement.hard_zero} 期望 {expect['hard_zero']}")
    if "attitude_dispute" in expect and panel["attitude_dispute"] != expect["attitude_dispute"]:
        problems.append(f"态度分歧={panel['attitude_dispute']} 期望 {expect['attitude_dispute']}")
    for key in ("disputes", "overrides", "revised"):
        if key in expect and len(panel[key]) != int(expect[key]):
            problems.append(f"{key} 条数={len(panel[key])} 期望 {expect[key]}")
    if "met" in expect:
        want = {item_key(raw, case_id=case_id) for raw in expect["met"] or []}
        got = {(m["dept"], it["item"]) for m in settlement.match for it in m["items"] if it["met"]}
        got |= {("认真程度", it["item"]) for it in settlement.effort if it["met"]}
        if got != want:
            problems.append(f"最终达成项={sorted(got)} 期望 {sorted(want)}")
    return problems


async def run_suite(path: Path, *, distribution: bool = False, **_: Any) -> SuiteResult:
    from official_agent.evaluation.panel.aggregate import settle

    data = _load_yaml(path) or {}
    cases: list[CaseResult] = []
    for case in data.get("cases") or []:
        case_id = str(case["id"])
        settlement = settle(
            judgement_of(case),
            critique=_critique_of(case),
            rebuttal=_rebuttal_of(case),
            first_dept=case.get("first_dept"),
        )
        problems = check_settlement(settlement, case.get("expect") or {}, case_id=case_id)
        cases.append(CaseResult(id=case_id, passed=not problems, detail=";".join(problems)))

    failed = [c for c in cases if not c.passed]
    return SuiteResult(
        name=path.stem,
        kind="panel_probes",
        source=path.name,
        status="FAIL" if failed else "PASS",
        cases=cases,
        notes=[f"通过 {len(cases) - len(failed)}/{len(cases)}"],
    )
