"""LLM-as-judge executor(#155/#62):四维 1-5 分报告——**只报告不阻塞**。

数据集 evals/datasets/qbank_judge.yaml:标准 dossier fixture + 题组。
无 LLM 配置 → SKIP;有 LLM → 跑 judge,套件恒 PASS(分数进 metrics/报告),
阈值等 AG8(#156)校准后由基线/门禁接管。
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _ts_blocker

    return _ts_blocker()


async def run_suite(path: Path, *, distribution: bool = False, **_: Any) -> SuiteResult:
    from official_agent.config import get_effective_settings
    from official_agent.evaluation.judge import judge_qbank
    from official_agent.graphs.assistant import build_model

    data = _load_yaml(path) or {}
    dossier = str((data.get("fixtures") or {}).get("dossier", ""))
    group = (data.get("fixtures") or {}).get("group") or {}

    settings = get_effective_settings()
    model = build_model(settings, temperature=0.0)
    try:
        report = await judge_qbank(dossier, group, model=model)
    except Exception as exc:  # noqa: BLE001 — 报告模式:judge 崩不中止整个 eval run
        return SuiteResult(
            name=path.stem,
            kind="qbank_judge",
            source=path.name,
            status="SKIP",
            notes=[f"judge 执行失败(报告模式不阻塞): {type(exc).__name__}: {exc}"[:120]],
        )

    cases = [
        CaseResult(
            id=d["dimension"],
            passed=1 <= d["score"] <= 5,
            detail=f"{d['score']}/5 {d['reason'][:60]}",
        )
        for d in report["dimensions"]
    ]
    metrics = {f"judge_{d['dimension']}": float(d["score"]) for d in report["dimensions"]}

    # 报告落盘(#155 契约):AG8 校准与人工抽查的数据源;fail-open
    report_path = path.parent.parent / "last_qbank_judge_report.json"
    with contextlib.suppress(OSError):
        report_path.write_text(
            json.dumps({"suite": str(path), "report": report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return SuiteResult(
        name=path.stem,
        kind="qbank_judge",
        source=path.name,
        status="PASS",  # 报告模式:恒不 FAIL(阈值校准后由 AG8 决定门禁)
        cases=cases,
        metrics=metrics,
        notes=[f"judge 报告模式(只报告不阻塞): overall={report.get('overall', '')[:80]}"],
    )
