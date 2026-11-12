"""规划探针 executor:预算上限不被突破(确定性)+ 规划者定档合不合理(真实模型)。

数据集 evals/datasets/plan_probes.yaml 分两段:

- 确定性段:整包封顶、规划输出的收口、越界输出的拒收——都是 evaluation/plan.py
  里的纯函数,不调模型,任何环境都跑,任何一条不过 suite 就 FAIL;
- 模型段:真实规划者给合成的候选仓库定档,对照每个仓可接受的档位。没有 LLM 配置
  时逐条记为跳过、不计分,并在备注里写明——此时 PASS 只代表确定性段。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evaluation import plan as plan_mod
from official_agent.evaluation.plan import LinePlan, PlannedLine, RepoCandidate

#: 规划调用的签名(测试里换成替身)
Planner = Callable[..., Awaitable[tuple[list[PlannedLine], Any]]]


def _load_yaml(path: Path) -> Any:
    """同步读盘抽小函数(异步体内不做阻塞 IO)。"""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None  # 确定性段没有环境依赖;模型段缺配置时在 suite 内部逐条跳过


def _llm_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _blocker

    return _blocker()


def _cap_case(case: dict[str, Any]) -> CaseResult:
    lines = tuple(LinePlan(line_id=f"line{i}", tier=tier) for i, tier in enumerate(case["tiers"]))
    max_turns, max_seconds = int(case["max_total_turns"]), int(case["max_total_wall_seconds"])
    capped = plan_mod.cap_lines(
        lines, max_total_turns=max_turns, max_total_wall_seconds=max_seconds
    )
    tiers = [line.tier for line in capped]
    turns = sum(line.budget.max_turns for line in capped)
    seconds = sum(line.budget.max_wall_seconds for line in capped)
    within_caps = turns <= max_turns and seconds <= max_seconds
    return CaseResult(
        id=str(case["id"]),
        passed=within_caps and tiers == list(case["expect"]),
        detail=f"{tiers}(合计 {turns} 轮 / {seconds} 秒)",
    )


def _settle_case(case: dict[str, Any]) -> CaseResult:
    candidates = [
        RepoCandidate(owner=line_id.split("/")[0], name=line_id.split("/")[1])
        for line_id in case["candidates"]
    ]
    planned = [PlannedLine(**line) for line in case.get("planned") or []]
    settled = {line.line_id: line.tier for line in plan_mod.settle_lines(candidates, planned)}
    return CaseResult(
        id=str(case["id"]), passed=settled == dict(case["expect"]), detail=str(settled)
    )


def _reject_case(case: dict[str, Any]) -> CaseResult:
    try:
        plan_mod.parse_plan(json.dumps(case["output"], ensure_ascii=False))
    except ValueError as exc:
        return CaseResult(id=str(case["id"]), passed=True, detail=f"已拒收:{str(exc)[:60]}")
    return CaseResult(id=str(case["id"]), passed=False, detail="越界输出被接受了")


async def _planner_case(case: dict[str, Any], planner: Planner) -> CaseResult:
    candidates = [RepoCandidate(**repo) for repo in case["repos"]]
    try:
        planned, _usage = await planner(candidates, str(case.get("statement", "")))
    except Exception as exc:  # noqa: BLE001 — 单条用例的模型失败记为不过,不中止整个 suite
        return CaseResult(
            id=str(case["id"]), passed=False, detail=f"规划调用失败:{type(exc).__name__}"
        )
    tiers = {line.line_id: line.tier for line in plan_mod.settle_lines(candidates, planned)}
    acceptable = {line_id: set(allowed) for line_id, allowed in case["expect"].items()}
    passed = all(tiers.get(line_id) in allowed for line_id, allowed in acceptable.items())
    return CaseResult(id=str(case["id"]), passed=passed, detail=str(tiers))


async def run_suite(
    path: Path,
    *,
    distribution: bool = False,
    planner: Planner = plan_mod.ask_planner,
    llm_blocker: Callable[[], str | None] = _llm_blocker,
    **_: Any,
) -> SuiteResult:
    data = _load_yaml(path) or {}
    defaults = data.get("defaults") or {}

    deterministic = [
        *(_cap_case(case) for case in data.get("cap_cases") or []),
        *(_settle_case(case) for case in data.get("settle_cases") or []),
        *(_reject_case(case) for case in data.get("reject_cases") or []),
    ]
    deterministic_ok = all(case.passed for case in deterministic)
    metrics = {
        "deterministic_pass_rate": (
            sum(case.passed for case in deterministic) / len(deterministic)
            if deterministic
            else 1.0
        )
    }

    planner_cases = data.get("planner_cases") or []
    blocker = llm_blocker()
    notes: list[str] = []
    if blocker:
        # 跳过的用例按引擎的约定记 passed=True + skipped=True:不进通过率的分母,
        # 报告里也不会被列成失败
        model_results = [
            CaseResult(id=str(case["id"]), passed=True, detail=f"未跑:{blocker}", skipped=True)
            for case in planner_cases
        ]
        model_ok = True
        notes.append(
            f"模型段 {len(planner_cases)} 条未跑({blocker}),不计分;PASS 只代表确定性段"
        )
    else:
        model_results = [await _planner_case(case, planner) for case in planner_cases]
        rate = (
            sum(case.passed for case in model_results) / len(model_results)
            if model_results
            else 1.0
        )
        metrics["model_pass_rate"] = rate
        model_ok = distribution or rate >= float(defaults.get("min_model_pass_rate", 0.0))

    return SuiteResult(
        name=path.stem,
        kind="plan_probes",
        source=path.name,
        status="PASS" if deterministic_ok and model_ok else "FAIL",
        cases=[*deterministic, *model_results],
        metrics=metrics,
        notes=notes,
    )
