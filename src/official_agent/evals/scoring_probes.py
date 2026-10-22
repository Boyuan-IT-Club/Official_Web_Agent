"""简历初筛评分门禁执行器:合成简历跑评分子图,逐条核对期望性质。

数据集 evals/datasets/scoring_probes.yaml。真实 LLM 驱动,无 key → SKIP。
断言性质而非精确分数(区间、调剂部门、面试提示片段、是否重点复核);
套件通过率低于 min_pass_rate → FAIL。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult

#: 评分入口的形状(测试注入假实现):fields + profile → 卡 dict
ScoreFn = Callable[..., Awaitable[dict[str, Any]]]


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


def check_card(card: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """卡 vs 期望 → 不满足的条目(空表 = 通过)。纯函数,单测直接喂卡。"""
    problems: list[str] = []
    if "hard_zero" in expect and bool(card.get("hard_zero")) != bool(expect["hard_zero"]):
        problems.append(f"hard_zero={card.get('hard_zero')} 期望 {expect['hard_zero']}")

    def _within(name: str, value: Any, low: Any, high: Any) -> None:
        if low is None and high is None:
            return
        if value is None:
            problems.append(f"{name} 缺失")
            return
        if low is not None and value < float(low):
            problems.append(f"{name}={value} < {low}")
        if high is not None and value > float(high):
            problems.append(f"{name}={value} > {high}")

    _within("match", card.get("match_score"), expect.get("match_min"), expect.get("match_max"))
    _within(
        "effort", card.get("effort_score"), expect.get("effort_min"), expect.get("effort_max")
    )
    if "transfer" in expect:
        got = (card.get("transfer_hint") or {}).get("dept")
        if got != expect["transfer"]:
            problems.append(f"调剂建议={got} 期望 {expect['transfer']}")
    hints = " ".join(card.get("interview_hints") or [])
    for fragment in expect.get("hints_contain") or []:
        if fragment not in hints:
            problems.append(f"面试提示缺「{fragment}」")
    return problems


async def run_suite(
    path: Path, *, score: ScoreFn | None = None, distribution: bool = False, **_: Any
) -> SuiteResult:
    from official_agent.evaluation.applicant import ApplicantProfile

    if score is None:
        from official_agent.evaluation.graph import run_evaluation

        score = run_evaluation

    data = _load_yaml(path) or {}
    min_pass_rate = float((data.get("defaults") or {}).get("min_pass_rate", 0.85))
    cases: list[CaseResult] = []
    for i, case in enumerate(data.get("cases") or []):
        raw = case.get("profile") or {}
        profile = ApplicantProfile(
            first_dept=raw.get("first_dept"),
            second_dept=raw.get("second_dept"),
            major=str(raw.get("major") or ""),
            grade=str(raw.get("grade") or ""),
        )
        fields = [
            {"field_key": f"f{j}", "title": f.get("title", ""), "value": str(f.get("value", ""))}
            for j, f in enumerate(case.get("fields") or [])
        ]
        try:
            card = await score(fields, resume_id=-(i + 1), cycle_id=0, profile=profile)
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            cases.append(
                CaseResult(id=case["id"], passed=False, detail=f"{type(exc).__name__}: {exc}"[:160])
            )
            continue
        problems = check_card(card, case.get("expect") or {})
        detail = (
            f"match={card.get('match_score')} effort={card.get('effort_score')} "
            f"transfer={(card.get('transfer_hint') or {}).get('dept')}"
        )
        cases.append(
            CaseResult(
                id=case["id"],
                passed=not problems,
                detail=detail if not problems else f"{detail};" + ";".join(problems),
            )
        )

    passed = sum(1 for c in cases if c.passed)
    rate = passed / len(cases) if cases else 1.0
    return SuiteResult(
        name=path.stem,
        kind="scoring_probes",
        source=path.name,
        status="PASS" if rate >= min_pass_rate else "FAIL",
        cases=cases,
        metrics={"scoring_pass_rate": round(rate, 3)},
        notes=[f"通过 {passed}/{len(cases)},门槛 {min_pass_rate}"],
    )


def main(argv: list[str] | None = None) -> int:
    """部署后冒烟入口:`python -m official_agent.evals.scoring_probes <yaml>`。

    镜像里不带 evals/ 目录,CD 把数据集同步到服务器后从这里跑;退出码 0 通过、
    1 未达门槛,只做报告,由调用方决定是否阻断。
    """
    import asyncio
    import sys

    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("用法:python -m official_agent.evals.scoring_probes <scoring_probes.yaml>")
        return 2
    blocker = env_blocker()
    if blocker:
        print(f"SKIP:{blocker}")
        return 0
    result = asyncio.run(run_suite(Path(args[0])))
    print(f"{result.status} {result.metrics} {' '.join(result.notes)}")
    for case in result.cases:
        print(f"  {'PASS' if case.passed else 'FAIL'} {case.id} {case.detail}")
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
