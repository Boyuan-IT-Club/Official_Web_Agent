"""题型探针 executor 单测:确定性段的判定、模型段的跳过与判定(假归纳者,不调模型)。"""

from pathlib import Path

import yaml

from official_agent.evals import exemplar_probes as probes
from official_agent.evals.engine import discover_suites
from official_agent.evaluation.exemplar import InducedPattern

_EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
_DATASET = _EVALS_DIR / "datasets" / "exemplar_probes.yaml"
_CATEGORY = "C2_技术选型与权衡"


def _no_llm() -> str:
    return "LLM 未配置"


def _llm_ready() -> None:
    return None


def _inducer_with(*patterns: str, source_ids: tuple[str, ...] = ("p1", "p2", "p3")):
    """假归纳者:给什么题型文字就归纳出什么,来源固定。"""

    async def _induce(picked):
        return [
            InducedPattern(pattern=text, category=_CATEGORY, source_ids=list(source_ids))
            for text in patterns
        ]

    return _induce


_CLEAN = "让候选人说明某次更换实现方案的触发条件,以及当时依据的数据"


async def test_deterministic_segment_passes_and_model_segment_is_reported_as_skipped() -> None:
    """没有 LLM 配置:确定性段照跑照判;模型段跳过,备注里写明 PASS 只代表前者。"""

    async def _boom(picked):
        raise AssertionError("没有 LLM 配置时不该调归纳模型")

    result = await probes.run_suite(_DATASET, inducer=_boom, llm_blocker=_no_llm)

    skipped = [case for case in result.cases if case.skipped]
    scored = [case for case in result.cases if not case.skipped]
    assert result.status == "PASS"
    assert len(skipped) == 1 and len(scored) >= 8
    assert all(case.passed for case in scored)
    assert result.metrics == {"deterministic_pass_rate": 1.0}
    assert any("模型段 1 条未跑" in note and "只代表确定性段" in note for note in result.notes)


async def test_deidentified_induction_passes() -> None:
    result = await probes.run_suite(
        _DATASET, inducer=_inducer_with(_CLEAN), llm_blocker=_llm_ready
    )

    assert result.status == "PASS"
    assert result.metrics["model_pass_rate"] == 1.0


async def test_pattern_that_keeps_a_project_name_fails() -> None:
    """归纳结果里还留着原题埋的项目名:即使过得了闸也算泄露。"""
    leaking = "让候选人说明 club-site 更换限流方案的触发条件"

    gated = await probes.run_suite(_DATASET, inducer=_inducer_with(leaking), llm_blocker=_llm_ready)
    observed = await probes.run_suite(
        _DATASET, inducer=_inducer_with(leaking), llm_blocker=_llm_ready, distribution=True
    )

    assert gated.status == "FAIL" and gated.metrics["model_pass_rate"] == 0.0
    assert "club-site" in gated.cases[-1].detail
    assert observed.status == "PASS"  # 分布模式只记录不设门


async def test_induction_that_yields_nothing_usable_fails() -> None:
    """只归纳出够不上门槛的题型(来源只有一位面试官的一道题):等于没归纳出来。"""
    result = await probes.run_suite(
        _DATASET, inducer=_inducer_with(_CLEAN, source_ids=("p1",)), llm_blocker=_llm_ready
    )

    assert result.status == "FAIL"


async def test_inducer_error_fails_its_case_without_aborting_the_suite() -> None:
    async def _down(picked):
        raise RuntimeError("upstream 429")

    result = await probes.run_suite(_DATASET, inducer=_down, llm_blocker=_llm_ready)

    assert result.status == "FAIL"
    assert result.metrics["deterministic_pass_rate"] == 1.0


async def test_broken_gate_expectation_fails_even_in_distribution_mode(tmp_path: Path) -> None:
    """确定性段钉的是入库前的闸:任何一条不过都 FAIL,分布模式也不放行。"""
    path = tmp_path / "exemplar_probes.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "runner": "exemplar_probes",
                "gate_cases": [
                    {
                        "id": "带文件名却期望收下",
                        "pattern": "解释 app.py 的限流",
                        "expect": "accept",
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    result = await probes.run_suite(path, distribution=True, llm_blocker=_no_llm)

    assert result.status == "FAIL"


def test_shipped_dataset_is_wired() -> None:
    refs = {ref.name: ref for ref in discover_suites(_EVALS_DIR)}
    assert refs["exemplar_probes"].kind == "exemplar_probes"

    data = yaml.safe_load(_DATASET.read_text(encoding="utf-8"))
    for segment in ("gate_cases", "settle_cases", "induce_cases"):
        ids = [case["id"] for case in data[segment]]
        assert ids and len(ids) == len(set(ids)), segment
