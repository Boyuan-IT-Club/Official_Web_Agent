"""回复忠实度核对门禁执行器:掺了无依据或矛盾说法的回复要被认出来,忠实的回复不该被冤枉。

数据集 evals/datasets/kb_ops_faithfulness.yaml。真实 LLM 驱动(无 key → SKIP):每条
用例把写死的问题、回复与官方原文块交给核对角色,模型输出再经代码复核与计数,和
产品里走的是同一条路(judge_turn → review_claims → score_turn)。不读库不落库。

两个指标都是越高越好:
- unfaithful_detection_rate:掺了问题的回复里,被判为「不是每条都有依据」的比例;
- faithful_pass_rate:忠实的回复里,被判为「每条都有依据」的比例。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evals.kb_ops_fixtures import chunk_records


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


def check_score(score: Any, expect: dict[str, Any]) -> list[str]:
    """一轮的计数 vs 期望 → 不满足的条目(空表 = 通过)。纯函数。"""
    problems: list[str] = []
    if "faithful" in expect and score.faithful != bool(expect["faithful"]):
        problems.append(f"faithful={score.faithful} 期望 {expect['faithful']}")
    if expect.get("has_contradiction") and not score.contradicted:
        problems.append("没有认出与原文矛盾的说法")
    if "claims" in expect and score.total != int(expect["claims"]):
        problems.append(f"说法数={score.total} 期望 {expect['claims']}")
    if "min_claims" in expect and score.total < int(expect["min_claims"]):
        problems.append(f"说法数={score.total} 少于 {expect['min_claims']}")
    return problems


async def run_suite(
    path: Path, *, model: Any = None, distribution: bool = False, **_: Any
) -> SuiteResult:
    from official_agent.kb_ops.faithfulness import judge_turn, review_claims, score_turn

    if model is None:
        from official_agent.config import get_effective_settings
        from official_agent.evaluation.llm_common import TEMPERATURE_SCORING
        from official_agent.graphs.assistant import build_model

        model = build_model(get_effective_settings(), temperature=TEMPERATURE_SCORING)

    data = _load_yaml(path) or {}
    defaults = data.get("defaults") or {}
    min_detection = float(defaults.get("min_unfaithful_detection_rate", 0.9))
    min_pass = float(defaults.get("min_faithful_pass_rate", 0.8))
    all_chunks = chunk_records(data.get("chunks") or {})

    cases: list[CaseResult] = []
    flawed_total = flawed_caught = clean_total = clean_passed = 0
    for case in data.get("cases") or []:
        chunks = [all_chunks[i] for i in case["evidence"]]
        judged = True
        try:
            output, _usage = await judge_turn(
                str(case["question"]), str(case["reply"]), chunks, model=model
            )
            score = score_turn(review_claims(output, {c.chunk_id: c for c in chunks}))
            problems = check_score(score, case.get("expect") or {})
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            judged = False
            problems = [f"{type(exc).__name__}: {exc}"[:160]]
        if case.get("flawed"):
            flawed_total += 1
            # 核对调用出错不算「认出来了」
            flawed_caught += judged and score.total > 0 and not score.faithful
        elif case.get("counted", True):
            clean_total += 1
            clean_passed += judged and score.faithful
        cases.append(CaseResult(id=case["id"], passed=not problems, detail=";".join(problems)))

    detection = flawed_caught / flawed_total if flawed_total else 1.0
    clean_pass = clean_passed / clean_total if clean_total else 1.0
    return SuiteResult(
        name=path.stem,
        kind="kb_ops_faithfulness",
        source=path.name,
        status="PASS" if detection >= min_detection and clean_pass >= min_pass else "FAIL",
        cases=cases,
        metrics={
            "unfaithful_detection_rate": round(detection, 3),
            "faithful_pass_rate": round(clean_pass, 3),
        },
        notes=[
            f"掺了问题的回复 {flawed_caught}/{flawed_total} 被认出(门槛 {min_detection});"
            f"忠实的回复 {clean_passed}/{clean_total} 被判为全部有依据(门槛 {min_pass})"
        ],
    )
