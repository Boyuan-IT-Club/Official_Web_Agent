"""跨周期对比(retrospective/compare.py)单测:差值由代码算,跨版本拒绝对比。"""

import copy

import pytest

from official_agent.retrospective.compare import SchemaMismatchError, with_baseline


def _metric(metric_id: str, value, unit: str = "count", *, suppressed: bool = False) -> dict:
    return {
        "id": metric_id,
        "metric": metric_id,
        "value": None if suppressed else value,
        "unit": unit,
        "n": None if suppressed else 100,
        "suppressed": suppressed,
        "key": None,
    }


def _snapshot(cycle_id: int, metrics: list[dict], schema: str = "cycle_metrics/v1") -> dict:
    return {"schema": schema, "cycle_id": cycle_id, "metrics": metrics}


def _by_id(snapshot: dict) -> dict[str, dict]:
    return {m["id"]: m for m in snapshot["metrics"]}


def test_count_gets_absolute_and_relative_change() -> None:
    current = _snapshot(13, [_metric("funnel.resumes_total", 236)])
    baseline = _snapshot(12, [_metric("funnel.resumes_total", 200)])
    got = _by_id(with_baseline(current, baseline))["funnel.resumes_total"]
    assert (got["prev"], got["delta"], got["delta_ratio"]) == (200, 36, 0.18)


def test_ratio_gets_point_difference_only() -> None:
    """两个百分比相除容易读错(20% → 25% 是 +5 个百分点,不是 +25%),所以不给相对变化。"""
    current = _snapshot(13, [_metric("kb.not_found_rate", 0.25, "ratio")])
    baseline = _snapshot(12, [_metric("kb.not_found_rate", 0.2, "ratio")])
    got = _by_id(with_baseline(current, baseline))["kb.not_found_rate"]
    assert (got["prev"], got["delta"], got["delta_ratio"]) == (0.2, 0.05, None)


def test_metric_missing_from_baseline_has_no_delta() -> None:
    current = _snapshot(13, [_metric("conversation.tool_calls[new_tool]", 40)])
    got = _by_id(with_baseline(current, _snapshot(12, [])))["conversation.tool_calls[new_tool]"]
    assert (got["prev"], got["delta"], got["delta_ratio"]) == (None, None, None)


@pytest.mark.parametrize(
    ("current_suppressed", "baseline_suppressed"), [(True, False), (False, True), (True, True)]
)
def test_suppressed_side_yields_no_delta(
    current_suppressed: bool, baseline_suppressed: bool
) -> None:
    """任一侧被抑制都不出差值——有了差值和另一侧的数,被抑制的数就能反推。"""
    metric_id = "screening.rejected_by_dept[项目部]"
    current = _snapshot(13, [_metric(metric_id, 9, suppressed=current_suppressed)])
    baseline = _snapshot(12, [_metric(metric_id, 6, suppressed=baseline_suppressed)])
    got = _by_id(with_baseline(current, baseline))[metric_id]
    assert got["delta"] is None and got["delta_ratio"] is None


def test_zero_baseline_has_no_relative_change() -> None:
    current = _snapshot(13, [_metric("cost.evaluation_tokens", 500, "tokens")])
    baseline = _snapshot(12, [_metric("cost.evaluation_tokens", 0, "tokens")])
    got = _by_id(with_baseline(current, baseline))["cost.evaluation_tokens"]
    assert (got["prev"], got["delta"], got["delta_ratio"]) == (0, 500, None)


def test_different_metric_schema_is_refused() -> None:
    current = _snapshot(13, [_metric("funnel.resumes_total", 236)], schema="cycle_metrics/v2")
    baseline = _snapshot(12, [_metric("funnel.resumes_total", 200)], schema="cycle_metrics/v1")
    with pytest.raises(SchemaMismatchError, match="cycle_metrics/v2.*cycle_metrics/v1"):
        with_baseline(current, baseline)


def test_inputs_are_not_mutated_and_baseline_is_named() -> None:
    current = _snapshot(13, [_metric("funnel.resumes_total", 236)])
    baseline = _snapshot(12, [_metric("funnel.resumes_total", 200)])
    before = copy.deepcopy(current)
    result = with_baseline(current, baseline)
    assert current == before  # 存下来的快照正文不被读路径改动
    assert result["baseline"] == {"cycle_id": 12, "schema": "cycle_metrics/v1"}


def _topic(key: str, label: str, value: int) -> dict:
    return {**_metric(f"kb.top_topics[{key}]", value), "metric": "kb.top_topics", "label": label}


def test_ranked_text_items_are_compared_only_when_the_text_is_the_same() -> None:
    """清单项按名次编号:两届的「第一名」通常不是同一个主题,硬比出来的差值是错的。"""
    current = _snapshot(
        13, [_topic("t01", "招新报名截止时间", 120), _topic("t02", "面试地点与形式", 64)]
    )
    baseline = _snapshot(
        12, [_topic("t01", "招新报名截止时间", 90), _topic("t02", "各部门日常工作", 50)]
    )
    got = _by_id(with_baseline(current, baseline))

    same = got["kb.top_topics[t01]"]
    assert (same["prev"], same["delta"]) == (90, 30)
    other = got["kb.top_topics[t02]"]
    assert (other["prev"], other["delta"], other["delta_ratio"]) == (None, None, None)
