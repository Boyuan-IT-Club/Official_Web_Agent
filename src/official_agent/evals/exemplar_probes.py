"""题型探针 executor:入库前的两道关(确定性)+ 归纳结果去没去标识(真实模型)。

数据集 evals/datasets/exemplar_probes.yaml 分两段:

- 确定性段:题型文字的闸、计数与门槛——都是 evaluation/exemplar.py 里的纯函数,
  不调模型,任何环境都跑,任何一条不过 suite 就 FAIL;
- 模型段:真实模型从合成的「被选用的题」里归纳题型,检查归纳结果里没有原题埋的
  项目细节、每条都过得了闸、且至少有若干条够得上门槛。没有 LLM 配置时逐条记为
  跳过、不计分,并在备注里写明——此时 PASS 只代表确定性段。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evaluation import exemplar as exemplar_mod
from official_agent.evaluation.exemplar import InducedPattern, PickedQuestion

#: 归纳调用的签名(测试里换成替身)
Inducer = Callable[[list[PickedQuestion]], Awaitable[list[InducedPattern]]]

_CATEGORY = "C4_实现细节拷打"
#: 计数用例里用的题型文字:本身过得了闸,用例测的是计数与门槛
_CLEAN_PATTERN = "让候选人说明某次更换实现方案的触发条件"


def _load_yaml(path: Path) -> Any:
    """同步读盘抽小函数(异步体内不做阻塞 IO)。"""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None  # 确定性段没有环境依赖;模型段缺配置时在 suite 内部逐条跳过


def _llm_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _blocker

    return _blocker()


def _picked(rows: list[dict[str, Any]]) -> list[PickedQuestion]:
    return [
        PickedQuestion(
            pick_id=str(row["id"]),
            ref_id=f"ref-{row['id']}",
            resume_id=int(row["resume"]),
            interviewer_user_id=int(row["interviewer"]),
            category=str(row.get("category", _CATEGORY)),
            question=str(row.get("question", "")),
        )
        for row in rows
    ]


def _gate_case(case: dict[str, Any]) -> CaseResult:
    reason = exemplar_mod.gate_pattern(str(case["pattern"]))
    if case["expect"] == "accept":
        return CaseResult(id=str(case["id"]), passed=reason is None, detail=reason or "已收")
    passed = reason is not None and str(case.get("match", "")) in reason
    return CaseResult(id=str(case["id"]), passed=passed, detail=reason or "没有拒收")


def _settle_case(case: dict[str, Any]) -> CaseResult:
    induced = [
        InducedPattern(
            pattern=_CLEAN_PATTERN, category=_CATEGORY, source_ids=list(case["source_ids"])
        )
    ]
    proposed, rejected = exemplar_mod.settle_patterns(_picked(case["picks"]), induced)
    if case["expect"] == "rejected":
        detail = rejected[0]["reason"] if rejected else "被收下了"
        return CaseResult(id=str(case["id"]), passed=not proposed, detail=detail)
    counts = (
        {
            "picks": proposed[0].pick_count,
            "resumes": proposed[0].resume_count,
            "interviewers": proposed[0].interviewer_count,
        }
        if proposed
        else {}
    )
    return CaseResult(id=str(case["id"]), passed=counts == dict(case["expect"]), detail=str(counts))


async def _induce_case(case: dict[str, Any], inducer: Inducer) -> CaseResult:
    picked = _picked(case["picks"])
    try:
        induced = await inducer(picked)
    except Exception as exc:  # noqa: BLE001 — 单条用例的模型失败记为不过,不中止整个 suite
        return CaseResult(
            id=str(case["id"]), passed=False, detail=f"归纳调用失败:{type(exc).__name__}"
        )
    leaked = sorted(
        {
            str(token)
            for pattern in induced
            for token in case.get("forbidden") or []
            if str(token) in pattern.pattern
        }
    )
    gated = [reason for p in induced if (reason := exemplar_mod.gate_pattern(p.pattern))]
    proposed, _rejected = exemplar_mod.settle_patterns(picked, induced)
    enough = len(proposed) >= int(case.get("min_proposed", 1))
    return CaseResult(
        id=str(case["id"]),
        passed=not leaked and not gated and enough,
        detail=f"归纳 {len(induced)} 条,成题型 {len(proposed)} 条;泄露 {leaked};闸拒 {gated}",
    )


async def run_suite(
    path: Path,
    *,
    distribution: bool = False,
    inducer: Inducer = exemplar_mod.induce_patterns,
    llm_blocker: Callable[[], str | None] = _llm_blocker,
    **_: Any,
) -> SuiteResult:
    data = _load_yaml(path) or {}
    defaults = data.get("defaults") or {}

    deterministic = [
        *(_gate_case(case) for case in data.get("gate_cases") or []),
        *(_settle_case(case) for case in data.get("settle_cases") or []),
    ]
    deterministic_ok = all(case.passed for case in deterministic)
    metrics = {
        "deterministic_pass_rate": (
            sum(case.passed for case in deterministic) / len(deterministic)
            if deterministic
            else 1.0
        )
    }

    induce_cases = data.get("induce_cases") or []
    blocker = llm_blocker()
    notes: list[str] = []
    if blocker:
        # 跳过的用例按引擎的约定记 passed=True + skipped=True:不进通过率的分母,
        # 报告里也不会被列成失败
        model_results = [
            CaseResult(id=str(case["id"]), passed=True, detail=f"未跑:{blocker}", skipped=True)
            for case in induce_cases
        ]
        model_ok = True
        notes.append(f"模型段 {len(induce_cases)} 条未跑({blocker}),不计分;PASS 只代表确定性段")
    else:
        model_results = [await _induce_case(case, inducer) for case in induce_cases]
        rate = (
            sum(case.passed for case in model_results) / len(model_results)
            if model_results
            else 1.0
        )
        metrics["model_pass_rate"] = rate
        model_ok = distribution or rate >= float(defaults.get("min_model_pass_rate", 0.0))

    return SuiteResult(
        name=path.stem,
        kind="exemplar_probes",
        source=path.name,
        status="PASS" if deterministic_ok and model_ok else "FAIL",
        cases=[*deterministic, *model_results],
        metrics=metrics,
        notes=notes,
    )
