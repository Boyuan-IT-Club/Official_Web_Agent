"""strict Pydantic 输出契约测试(仓库首个结构化输出范式)。"""

import pytest
from pydantic import ValidationError

from official_agent.evaluation.schema import (
    DEPARTMENTS,
    DEPT_MATCH_ITEMS,
    EFFORT_ITEMS,
    AttitudeVerdict,
    DeptMatchVerdict,
    ItemVerdict,
    ScorecardOutput,
    item_names,
)


def _items(items, met: bool = True) -> list[ItemVerdict]:
    return [ItemVerdict(item=n, met=met, quote="原文" if met else "", reason="依据") for n in items]


def _output(**over) -> ScorecardOutput:
    base = {
        "match": [
            DeptMatchVerdict(dept=d, items=_items(item_names(DEPT_MATCH_ITEMS[d])))
            for d in DEPARTMENTS
        ],
        "effort": _items(item_names(EFFORT_ITEMS)),
        "summary": "与技术部对得上,缺分享经历,面试重点问实现细节。",
        "attitude": AttitudeVerdict(verdict="sincere", reason="整体认真"),
    }
    base.update(over)
    return ScorecardOutput(**base)


def test_valid_output_parses() -> None:
    out = _output()
    assert [m.dept for m in out.match] == list(DEPARTMENTS)
    assert out.attitude.verdict == "sincere"


def test_checklist_weights_sum_to_ten() -> None:
    """清单权重和为 10:达成项权重之和直接就是 0-10 分,人能心算核对。"""
    for dept in DEPARTMENTS:
        assert sum(w for _, w in DEPT_MATCH_ITEMS[dept]) == 10, dept
    assert sum(w for _, w in EFFORT_ITEMS) == 10


def test_media_and_general_have_separate_checklists() -> None:
    """媒体部与综合部录入标准不同,各自一张清单。"""
    assert "媒体部" in DEPT_MATCH_ITEMS and "综合部" in DEPT_MATCH_ITEMS
    assert item_names(DEPT_MATCH_ITEMS["媒体部"]) != item_names(DEPT_MATCH_ITEMS["综合部"])


def test_item_reason_required() -> None:
    """未达成也必须写缺什么 —— 空理由等于没给复核线索。"""
    with pytest.raises(ValidationError):
        ItemVerdict(item="分享意愿", met=False, reason="")


def test_extra_field_rejected_strict() -> None:
    with pytest.raises(ValidationError):
        ItemVerdict(item="技术基础", met=True, reason="r", score=9)  # extra="forbid"


def test_summary_required() -> None:
    """整体理由是这份卡的结论面,不能只给清单不给人话。"""
    with pytest.raises(ValidationError):
        _output(summary="")


def test_bad_verdict_literal_rejected() -> None:
    with pytest.raises(ValidationError):
        AttitudeVerdict(verdict="okay", reason="x")


def test_checklists_min_length() -> None:
    with pytest.raises(ValidationError):
        _output(effort=[])
    with pytest.raises(ValidationError):
        _output(match=[])
