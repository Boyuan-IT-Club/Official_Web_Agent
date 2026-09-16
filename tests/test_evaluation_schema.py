"""strict Pydantic 输出契约测试(仓库首个结构化输出范式)。"""

import pytest
from pydantic import ValidationError

from official_agent.evaluation.schema import (
    TRAITS,
    AttitudeVerdict,
    ScorecardOutput,
    TraitVerdict,
)


def _trait(**over) -> dict:
    base = {"trait": "经验丰富", "met": True, "reason": "做过两个 Web 项目"}
    base.update(over)
    return base


def _all_traits(met: bool = True) -> list[TraitVerdict]:
    return [TraitVerdict(trait=t, met=met, reason="依据") for t in TRAITS]


def test_valid_output_parses() -> None:
    out = ScorecardOutput(
        traits=_all_traits(),
        summary="强在项目经历,弱在开源沉淀,面试重点问实现细节。",
        attitude=AttitudeVerdict(verdict="sincere", reason="整体认真"),
    )
    assert out.traits[0].met is True
    assert out.attitude.verdict == "sincere"


def test_trait_reason_required() -> None:
    """未达成也必须写缺什么 —— 空理由等于没给复核线索。"""
    with pytest.raises(ValidationError):
        TraitVerdict(trait="开源精神", met=False, reason="")


def test_extra_field_rejected_strict() -> None:
    with pytest.raises(ValidationError):
        TraitVerdict(**_trait(score=90))  # extra="forbid"


def test_summary_required() -> None:
    """整体理由是这份卡的结论面,不能只给清单不给人话。"""
    with pytest.raises(ValidationError):
        ScorecardOutput(
            traits=_all_traits(),
            summary="",
            attitude=AttitudeVerdict(verdict="sincere", reason="x"),
        )


def test_bad_verdict_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        AttitudeVerdict(verdict="okay", reason="x")


def test_traits_min_length() -> None:
    with pytest.raises(ValidationError):
        ScorecardOutput(
            traits=[], summary="x", attitude=AttitudeVerdict(verdict="sincere", reason="x")
        )  # noqa: E501
