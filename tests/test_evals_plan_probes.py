"""规划探针 executor 单测:确定性段的判定、模型段的跳过与门槛(假规划者,不调模型)。"""

from pathlib import Path

import yaml

from official_agent.evals import plan_probes as probes
from official_agent.evals.engine import discover_suites
from official_agent.evaluation.plan import PlannedLine

_EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
_DATASET = _EVALS_DIR / "datasets" / "plan_probes.yaml"


def _no_llm() -> str:
    return "LLM 未配置"


def _llm_ready() -> None:
    return None


def _planner_with(tiers: dict[str, str]):
    """假规划者:按给定的档位表回答;表里没有的仓不提。"""

    async def _plan(candidates, project_text):
        lines = [
            PlannedLine(id=c.line_id, tier=tiers[c.line_id])
            for c in candidates
            if c.line_id in tiers
        ]
        return lines, None

    return _plan


#: 随仓数据集模型段里每个仓的一个「可接受」答案
_GOOD_TIERS = {
    "alice/club-site": "high",
    "alice/flask": "low",
    "alice/flask-tutorial": "low",
    "bob/library": "high",
    "bob/awesome-python": "low",
    "opensrc/fastjson-lite": "normal",
}


async def test_deterministic_segment_passes_and_model_segment_is_reported_as_skipped() -> None:
    """没有 LLM 配置:确定性段照跑照判;模型段逐条跳过,备注里写明 PASS 只代表前者。"""

    async def _boom(*a, **k):
        raise AssertionError("没有 LLM 配置时不该调规划者")

    result = await probes.run_suite(_DATASET, planner=_boom, llm_blocker=_no_llm)

    skipped = [case for case in result.cases if case.skipped]
    scored = [case for case in result.cases if not case.skipped]
    assert result.status == "PASS"
    assert len(skipped) == 3 and len(scored) >= 10
    assert all(case.passed for case in scored)
    assert result.metrics == {"deterministic_pass_rate": 1.0}  # 没跑的模型段不出指标
    assert any("模型段 3 条未跑" in note and "只代表确定性段" in note for note in result.notes)


async def test_model_segment_passes_when_tiers_are_acceptable() -> None:
    result = await probes.run_suite(
        _DATASET, planner=_planner_with(_GOOD_TIERS), llm_blocker=_llm_ready
    )

    assert result.status == "PASS"
    assert result.metrics["model_pass_rate"] == 1.0
    assert not result.notes


async def test_fork_planned_high_fails_the_gate_but_not_distribution_mode() -> None:
    """把 fork 仓定成最高档是不可接受的答案:过不了门槛;分布模式只记录不设门。"""
    wrong = {**_GOOD_TIERS, "alice/flask": "high"}

    gated = await probes.run_suite(_DATASET, planner=_planner_with(wrong), llm_blocker=_llm_ready)
    observed = await probes.run_suite(
        _DATASET, planner=_planner_with(wrong), llm_blocker=_llm_ready, distribution=True
    )

    assert gated.status == "FAIL"
    assert gated.metrics["model_pass_rate"] == 2 / 3
    assert observed.status == "PASS" and observed.metrics["model_pass_rate"] == 2 / 3


async def test_unmentioned_repo_counts_as_high() -> None:
    """规划者漏掉的仓按最高档算:漏掉 fork 仓等于把它定成了最高档。"""
    forgot_fork = {k: v for k, v in _GOOD_TIERS.items() if k != "alice/flask"}

    result = await probes.run_suite(
        _DATASET, planner=_planner_with(forgot_fork), llm_blocker=_llm_ready
    )

    failed = [case.id for case in result.cases if not case.passed]
    assert failed == ["fork 与教程仓落在低档,主项目最高档"]


async def test_planner_error_fails_its_case_without_aborting_the_suite() -> None:
    async def _down(candidates, project_text):
        raise RuntimeError("upstream 429")

    result = await probes.run_suite(_DATASET, planner=_down, llm_blocker=_llm_ready)

    assert result.status == "FAIL"
    assert result.metrics["model_pass_rate"] == 0.0
    assert result.metrics["deterministic_pass_rate"] == 1.0


async def test_broken_invariant_fails_even_in_distribution_mode(tmp_path: Path) -> None:
    """确定性段钉的是安全属性:任何一条不过都 FAIL,分布模式也不放行。"""
    path = tmp_path / "plan_probes.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "runner": "plan_probes",
                "cap_cases": [
                    {
                        "id": "期望写错了",
                        "tiers": ["high", "high"],
                        "max_total_turns": 80,
                        "max_total_wall_seconds": 10_000,
                        "expect": ["high", "high"],  # 实际会砍掉第二条
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    result = await probes.run_suite(path, distribution=True, llm_blocker=_no_llm)

    assert result.status == "FAIL"
    assert result.metrics["deterministic_pass_rate"] == 0.0


def test_shipped_dataset_is_wired() -> None:
    refs = {ref.name: ref for ref in discover_suites(_EVALS_DIR)}
    assert refs["plan_probes"].kind == "plan_probes"

    data = yaml.safe_load(_DATASET.read_text(encoding="utf-8"))
    for segment in ("cap_cases", "settle_cases", "reject_cases", "planner_cases"):
        ids = [case["id"] for case in data[segment]]
        assert ids and len(ids) == len(set(ids)), segment
