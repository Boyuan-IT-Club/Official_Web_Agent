"""草稿审核门禁执行器:埋了问题的草稿要被查出来,没问题的草稿不该被误拒。

数据集 evals/datasets/kb_ops_verify.yaml。真实 LLM 驱动(无 key → SKIP):每条用例
把写死的草稿与官方原文块交给审核角色,模型输出再经代码复核与裁决,和产品里走的是
同一条路(verify_draft → apply_review → ruling)。不读库不落库。

两个指标都是越高越好:
- flaw_detection_rate:埋了无出处句或矛盾句的草稿里,最终没有被放行的比例;
- clean_pass_rate:没有问题的草稿里,最终被放行的比例(1 − 误拒率)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evals.kb_ops_fixtures import chunk_records, proposal_of
from official_agent.kb.store import SearchHit


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


def check_outcome(
    decision: str, verdicts: list[str], conflict_ids: list[str], expect: dict[str, Any]
) -> list[str]:
    """裁决、逐句结论、冲突 vs 期望 → 不满足的条目(空表 = 通过)。纯函数。"""
    problems: list[str] = []
    if "ruling" in expect and decision != expect["ruling"]:
        problems.append(f"裁决={decision} 期望 {expect['ruling']}")
    if "not_ruling" in expect and decision == expect["not_ruling"]:
        problems.append(f"裁决不应为 {decision}")
    if "verdicts" in expect and verdicts != list(expect["verdicts"]):
        problems.append(f"逐句结论={verdicts} 期望 {expect['verdicts']}")
    for source_id in expect.get("conflicts") or []:
        if source_id not in conflict_ids:
            problems.append(f"没有标出与 {source_id} 的冲突:{conflict_ids}")
    return problems


def _neighbors(spec: list[dict[str, Any]]) -> list[SearchHit]:
    return [
        SearchHit(
            source_id=str(n["source_id"]),
            title=str(n.get("title", "")),
            chunk_text=str(n["text"]),
            heading_path="",
            score=0.5,
        )
        for n in spec
    ]


async def run_suite(
    path: Path, *, model: Any = None, distribution: bool = False, **_: Any
) -> SuiteResult:
    from official_agent.kb_ops.verify import RULING_ACCEPT, apply_review, ruling, verify_draft

    if model is None:
        from official_agent.config import get_effective_settings
        from official_agent.evaluation.llm_common import TEMPERATURE_SCORING
        from official_agent.graphs.assistant import build_model

        model = build_model(get_effective_settings(), temperature=TEMPERATURE_SCORING)

    data = _load_yaml(path) or {}
    defaults = data.get("defaults") or {}
    min_detection = float(defaults.get("min_flaw_detection_rate", 0.9))
    min_clean_pass = float(defaults.get("min_clean_pass_rate", 0.8))
    chunks = chunk_records(data.get("chunks") or {})

    cases: list[CaseResult] = []
    flawed_total = flawed_caught = clean_total = clean_passed = 0
    for case in data.get("cases") or []:
        expect = case.get("expect") or {}
        try:
            proposal = proposal_of(case["draft"])
            review, _usage = await verify_draft(
                proposal, chunks, _neighbors(case.get("neighbors") or []), model=model
            )
            findings = apply_review(proposal, chunks, review)
            decision = ruling(findings)
            problems = check_outcome(
                decision,
                [f.verdict for f in findings],
                [c.source_id for c in review.conflicts],
                expect,
            )
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            decision = "error"
            problems = [f"{type(exc).__name__}: {exc}"[:160]]
        if case.get("flawed"):
            flawed_total += 1
            # 审核调用出错时草稿不会被放行,但那不是「查出来了」,不计入检出
            flawed_caught += decision not in (RULING_ACCEPT, "error")
        else:
            clean_total += 1
            clean_passed += decision == RULING_ACCEPT
        cases.append(CaseResult(id=case["id"], passed=not problems, detail=";".join(problems)))

    detection = flawed_caught / flawed_total if flawed_total else 1.0
    clean_pass = clean_passed / clean_total if clean_total else 1.0
    return SuiteResult(
        name=path.stem,
        kind="kb_ops_verify",
        source=path.name,
        status="PASS" if detection >= min_detection and clean_pass >= min_clean_pass else "FAIL",
        cases=cases,
        metrics={
            "flaw_detection_rate": round(detection, 3),
            "clean_pass_rate": round(clean_pass, 3),
        },
        notes=[
            f"埋了问题的草稿 {flawed_caught}/{flawed_total} 未被放行(门槛 {min_detection});"
            f"没问题的草稿 {clean_passed}/{clean_total} 被放行(门槛 {min_clean_pass})"
        ],
    )
