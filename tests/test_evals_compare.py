"""两组 eval 报告的逐条对照:翻转分类、重复运行按多数、分母只算两侧都有的用例。"""

from __future__ import annotations

import pytest

from official_agent.evals.compare import compare_reports, render


def _report(cases: dict[str, bool], **context: str) -> dict:
    return {
        "suites": [
            {
                "name": "tool_selection",
                "context": context,
                "observations": {"latency_p95_ms": 900},
                "cases": [{"id": cid, "passed": ok} for cid, ok in cases.items()],
            }
        ]
    }


def test_flips_are_classified_with_denominator() -> None:
    before = _report({"a": False, "b": True, "c": True, "d": False, "old": True}, intent_mode="off")
    after = _report({"a": True, "b": False, "c": True, "d": False, "new": True}, intent_mode="hint")
    (cmp,) = compare_reports([before], [after])
    assert cmp.fixed == ["a"] and cmp.broke == ["b"]
    assert cmp.still_pass == ["c"] and cmp.still_fail == ["d"]
    assert cmp.only_one_side == ["new", "old"]
    assert cmp.compared == 4
    text = render([cmp])
    assert "| 原错新对 | 1/4 | a(0→1) |" in text
    assert '"intent_mode": "hint"' in text


def test_repeated_runs_use_majority_and_ties_are_unstable() -> None:
    before = [_report({"a": False, "b": True}), _report({"a": False, "b": False})]
    after = [_report({"a": True, "b": True}), _report({"a": True, "b": True})]
    (cmp,) = compare_reports(before, after)
    assert cmp.fixed == ["a"]
    assert cmp.unstable == ["b"]  # 前侧 1/2 平票:不算修好
    assert cmp.tallies["b"] == (1, 2)


def test_empty_side_is_rejected() -> None:
    with pytest.raises(ValueError):
        compare_reports([], [_report({"a": True})])
