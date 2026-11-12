"""审题探针 executor 单测:指标口径与门槛判定(假审题者,不调模型)。"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from official_agent.evals import question_review_probes as probes
from official_agent.evals.engine import discover_suites

_EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
_DATASET = _EVALS_DIR / "datasets" / "question_review_probes.yaml"


def _suite(tmp_path: Path, **defaults: float) -> Path:
    """两道好题 + 两道植入坏题的小数据集。"""
    path = tmp_path / "question_review_probes.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "runner": "question_review_probes",
                "defaults": {
                    "min_planted_flag_rate": 0.5,
                    "min_good_not_dropped_rate": 1.0,
                    **defaults,
                },
                "fixtures": {"dossier": "D", "statement": "S"},
                "questions": [
                    {"key": "entry", "role": "entry", "question": "g1?", "expect": "good"},
                    {"key": "reserve:0", "role": "reserve", "question": "g2?", "expect": "good"},
                    {
                        "key": "reserve:1",
                        "role": "reserve",
                        "question": "b1?",
                        "expect": ["searchable", "recitable"],
                    },
                    {"key": "reserve:2", "role": "reserve", "question": "b2?", "expect": ["tone"]},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _reviewer(opinions: dict[str, tuple[str, list[str]]], *, status: str = "reviewed"):
    seen: dict[str, Any] = {}

    async def _review(targets, *, material, statement=""):
        seen.update(keys=[t.key for t in targets], material=material, statement=statement)
        return {
            "status": status,
            "items": [
                {"key": key, "verdict": verdict, "reasons": reasons}
                for key, (verdict, reasons) in opinions.items()
            ],
            "prompt_version": "evaluation_question_review/v1",
        }

    _review.seen = seen  # type: ignore[attr-defined]
    return _review


async def test_metrics_and_pass(tmp_path: Path) -> None:
    """坏题要被标出来且理由对得上;好题被打回不算丢弃,但会拉低 good_keep_rate。"""
    reviewer = _reviewer(
        {
            "entry": ("keep", []),
            "reserve:0": ("revise", ["recitable"]),  # 误伤:没丢,但没放行
            "reserve:1": ("drop", ["recitable"]),  # 标出,理由在可接受集合里
            "reserve:2": ("revise", ["generic"]),  # 标了,但理由不对 → 不算
        }
    )

    result = await probes.run_suite(_suite(tmp_path), reviewer=reviewer)

    assert result.status == "PASS"
    assert result.metrics == {
        "planted_flag_rate": 0.5,
        "good_not_dropped_rate": 1.0,
        "good_keep_rate": 0.5,
    }
    assert {c.id: c.passed for c in result.cases} == {
        "entry": True,
        "reserve:0": True,
        "reserve:1": True,
        "reserve:2": False,
    }
    assert reviewer.seen == {
        "keys": ["entry", "reserve:0", "reserve:1", "reserve:2"],
        "material": "D",
        "statement": "S",
    }


@pytest.mark.parametrize(
    ("opinions", "why"),
    [
        (
            {"reserve:0": ("drop", ["generic"]), "reserve:1": ("drop", ["searchable"])},
            "好题被判丢弃",
        ),
        ({}, "坏题一道都没标出来"),
    ],
)
async def test_gate_failures(tmp_path: Path, opinions, why) -> None:
    result = await probes.run_suite(_suite(tmp_path), reviewer=_reviewer(opinions))
    assert result.status == "FAIL", why


async def test_distribution_mode_reports_without_gating(tmp_path: Path) -> None:
    result = await probes.run_suite(_suite(tmp_path), distribution=True, reviewer=_reviewer({}))
    assert result.status == "PASS"
    assert result.metrics["planted_flag_rate"] == 0.0


async def test_unavailable_review_fails_the_suite(tmp_path: Path) -> None:
    """审题者没给出可用结果时不能当成「一道都没标」蒙混过去。"""
    result = await probes.run_suite(
        _suite(tmp_path), distribution=True, reviewer=_reviewer({}, status="unavailable")
    )
    assert result.status == "FAIL"
    assert not result.metrics


def test_shipped_dataset_is_wired_and_well_formed() -> None:
    """随仓的数据集能被引擎发现,题目编号不重复,期望值都在合法取值里。"""
    from typing import get_args

    from official_agent.evaluation.schema import REVIEW_REASON

    refs = {ref.name: ref for ref in discover_suites(_EVALS_DIR)}
    assert refs["question_review_probes"].kind == "question_review_probes"

    data = yaml.safe_load(_DATASET.read_text(encoding="utf-8"))
    questions = data["questions"]
    keys = [q["key"] for q in questions]
    assert len(keys) == len(set(keys))
    planted = [q for q in questions if q["expect"] != "good"]
    assert planted and len(planted) < len(questions)
    for q in planted:
        assert set(q["expect"]) <= set(get_args(REVIEW_REASON))
