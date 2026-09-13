"""B1 strict Pydantic 输出契约测试(#123:仓库首个结构化输出范式)。"""

import pytest
from pydantic import ValidationError

from official_agent.evaluation.schema import AttitudeVerdict, DimensionScore, ScorecardOutput


def _dim(**over) -> dict:
    base = {
        "field_key": "intro",
        "score": 75,
        "rationale": "内容具体",
        "evidence": "我做过两个 Web 项目",
    }
    base.update(over)
    return base


def test_valid_output_parses() -> None:
    out = ScorecardOutput(
        dimensions=[DimensionScore(**_dim())],
        attitude=AttitudeVerdict(verdict="sincere", reason="整体认真"),
    )
    assert out.dimensions[0].score == 75
    assert out.attitude.verdict == "sincere"


def test_score_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        DimensionScore(**_dim(score=101))
    with pytest.raises(ValidationError):
        DimensionScore(**_dim(score=-1))


def test_extra_field_rejected_strict() -> None:
    with pytest.raises(ValidationError):
        DimensionScore(**_dim(confidence=0.9))  # extra="forbid"


def test_bad_verdict_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        AttitudeVerdict(verdict="okay", reason="x")


def test_empty_evidence_rejected() -> None:
    with pytest.raises(ValidationError):
        DimensionScore(**_dim(evidence=""))
