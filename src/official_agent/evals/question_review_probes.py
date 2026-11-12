"""审题探针 executor:植入的坏题要被标出来,好题不能被判丢弃。

数据集 evals/datasets/question_review_probes.yaml:一组题混着好题与植入的坏题,
交给审题者(evaluation/question_review.py,真实模型)逐题给意见。三个指标,都是
越高越好:

- planted_flag_rate:坏题被标出来、且理由对得上的比例;
- good_not_dropped_rate:好题没有被判 drop 的比例;
- good_keep_rate:好题被判 keep 的比例——即没有误伤。审题从「只标注」改成
  「拦截」之前要看的就是它。

前两项过门槛才 PASS;审题者没给出可用结果直接 FAIL。无 LLM 配置 → SKIP。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evaluation.question_review import ReviewTarget, review_line

#: 审题入口的签名(测试里换成替身)
Reviewer = Callable[..., Awaitable[dict[str, Any] | None]]


def _load_yaml(path: Path) -> Any:
    """同步读盘抽小函数(异步体内不做阻塞 IO)。"""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


def _rate(hits: int, total: int) -> float:
    return hits / total if total else 1.0


async def run_suite(
    path: Path,
    *,
    distribution: bool = False,
    reviewer: Reviewer = review_line,
    **_: Any,
) -> SuiteResult:
    data = _load_yaml(path) or {}
    defaults = data.get("defaults") or {}
    fixtures = data.get("fixtures") or {}
    questions = data.get("questions") or []
    targets = [
        ReviewTarget(
            key=str(q["key"]),
            role=str(q.get("role", "question")),
            question=str(q["question"]),
            context=str(q.get("context", "")),
        )
        for q in questions
    ]
    review = await reviewer(
        targets,
        material=str(fixtures.get("dossier", "")),
        statement=str(fixtures.get("statement", "")),
    )
    if not review or review.get("status") != "reviewed":
        return SuiteResult(
            name=path.stem,
            kind="question_review_probes",
            source=path.name,
            status="FAIL",
            notes=["审题者没有给出可用结果(模型调用失败,或两次输出都不合规)"],
        )
    opinions = {item["key"]: item for item in review.get("items") or []}

    cases: list[CaseResult] = []
    planted = planted_flagged = good = good_not_dropped = good_kept = 0
    for q in questions:
        key = str(q["key"])
        opinion = opinions.get(key) or {"verdict": "keep", "reasons": []}
        verdict, reasons = opinion["verdict"], list(opinion.get("reasons") or [])
        detail = f"{verdict} {reasons}"
        if q.get("expect") == "good":
            good += 1
            good_not_dropped += verdict != "drop"
            good_kept += verdict == "keep"
            cases.append(CaseResult(id=key, passed=verdict != "drop", detail=detail))
            continue
        acceptable = {str(reason) for reason in q.get("expect") or []}
        flagged = verdict != "keep" and bool(acceptable & set(reasons))
        planted += 1
        planted_flagged += flagged
        cases.append(
            CaseResult(id=key, passed=flagged, detail=f"{detail}(期望 {sorted(acceptable)})")
        )

    metrics = {
        "planted_flag_rate": _rate(planted_flagged, planted),
        "good_not_dropped_rate": _rate(good_not_dropped, good),
        "good_keep_rate": _rate(good_kept, good),
    }
    meets_gate = metrics["planted_flag_rate"] >= float(
        defaults.get("min_planted_flag_rate", 0.0)
    ) and metrics["good_not_dropped_rate"] >= float(defaults.get("min_good_not_dropped_rate", 0.0))
    return SuiteResult(
        name=path.stem,
        kind="question_review_probes",
        source=path.name,
        status="PASS" if (distribution or meets_gate) else "FAIL",
        cases=cases,
        metrics=metrics,
        notes=[f"审题 prompt 版本:{review.get('prompt_version', '')}"],
    )
