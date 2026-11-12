"""报告的交接栏目(retrospective/handoff.py)单测:栏目只取快照里的东西,不调模型。

要守住的:进栏目的每个数都是过了抑制的;上游没有数据时栏目如实说明,不编、不报错。
"""

import json

import pytest
from tests.retro_helpers import make_snapshot, topic_row

from official_agent.retrospective import handoff


def _topics(*rows: dict) -> dict:
    return {
        "run": {
            "run_id": "kbops-test",
            "window_start": "2026-11-01T00:00:00+08:00",
            "window_end": "2026-11-30T00:00:00+08:00",
        },
        "topics": list(rows),
    }


async def test_gap_list_names_each_topic_with_its_count_and_gap_kind() -> None:
    got = handoff.kb_gap_list(await make_snapshot())

    assert got == {
        "status": "ok",
        "reason": None,
        "window": {"start": "2026-11-01T00:00:00+08:00", "end": "2026-11-30T00:00:00+08:00"},
        "items": [
            {"metric_id": "kb.top_topics[t01]", "topic": "招新报名截止时间", "searches": 120,
             "gap": "missing"},
            {"metric_id": "kb.top_topics[t02]", "topic": "面试地点与形式", "searches": 64,
             "gap": "wording"},
            {"metric_id": "kb.top_topics[t03]", "topic": "各部门日常工作", "searches": 30,
             "gap": "missing"},
        ],
    }


async def test_gap_list_never_shows_a_topic_asked_in_too_few_sessions() -> None:
    """栏目里的每一项都出自快照:没过小格子规则的主题,名字和次数都到不了这里。"""
    snapshot = await make_snapshot(
        kb_topics=_topics(
            topic_row(1, "招新报名截止时间", queries=120, threads=85),
            topic_row(2, "宿舍楼下的快递柜密码", queries=40, threads=3),
            topic_row(3, "只问过三次的事", queries=3, threads=3),
        )
    )
    got = handoff.kb_gap_list(snapshot)

    assert [item["topic"] for item in got["items"]] == ["招新报名截止时间"]
    text = json.dumps(got, ensure_ascii=False)
    assert "快递柜" not in text and "只问过三次" not in text
    assert all(item["searches"] >= 5 for item in got["items"])


async def test_gap_list_is_empty_not_absent_when_no_topic_qualifies() -> None:
    snapshot = await make_snapshot(
        kb_topics=_topics(topic_row(1, "很少人问的事", queries=4, threads=2))
    )
    got = handoff.kb_gap_list(snapshot)

    assert (got["status"], got["items"]) == ("empty", [])
    assert "不足以列出" in got["reason"]
    assert got["window"] is not None  # 分析跑过,只是没有主题够格:时间窗照样交代


async def test_gap_list_says_why_when_no_analysis_overlaps_the_cycle() -> None:
    got = handoff.kb_gap_list(await make_snapshot(kb_topics=None))

    assert got == {
        "status": "absent",
        "reason": "与周期时间窗有重叠的知识库离线分析一次都没有成功过",
        "window": None,
        "items": [],
    }


async def test_gap_list_says_why_when_the_topic_source_was_down() -> None:
    got = handoff.kb_gap_list(await make_snapshot(without=("kb_topics",)))

    assert got["status"] == "absent"
    assert got["reason"] == "主题清单取不到:合成快照没有提供这个域的数据"


async def test_gap_list_says_why_when_the_whole_kb_domain_was_down() -> None:
    got = handoff.kb_gap_list(await make_snapshot(without=("kb",)))

    assert (got["status"], got["reason"]) == ("absent", "合成快照没有提供这个域的数据")


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"metrics": [], "catalog": [], "absent": [], "domains": {}},
        # 指标定义里还没有高频主题的旧快照
        {"schema": "cycle_metrics/v1", "metrics": [{"id": "kb.searches", "metric": "kb.searches",
                                                    "value": 9, "suppressed": False}],
         "catalog": [{"id": "kb.searches", "domain": "kb", "status": "ok"}]},
        # 旧目录把它标成暂缺
        {"catalog": [{"id": "kb.top_topics", "domain": "kb", "status": "pending",
                      "reason": "主题聚类尚未实现"}]},
    ],
)
def test_gap_list_never_fails_on_a_snapshot_without_topics(snapshot: dict) -> None:
    got = handoff.kb_gap_list(snapshot)

    assert (got["status"], got["items"], got["window"]) == ("absent", [], None)
    assert got["reason"]


def test_gap_list_skips_a_suppressed_cell_instead_of_showing_a_blank_count() -> None:
    snapshot = {
        "metrics": [
            {"id": "kb.top_topics[t01]", "metric": "kb.top_topics", "label": "甲", "value": None,
             "suppressed": True},
            {"id": "kb.top_topics[t02]", "metric": "kb.top_topics", "label": "乙", "value": 9,
             "suppressed": False, "group": "wording"},
        ],
    }
    got = handoff.kb_gap_list(snapshot)

    assert got["items"] == [
        {"metric_id": "kb.top_topics[t02]", "topic": "乙", "searches": 9, "gap": "wording"}
    ]


def test_gap_kind_and_window_are_written_for_people() -> None:
    assert handoff.gap_label("missing") == "知识库里多半没有"
    assert handoff.gap_label("wording") == "多半有,问法没对上"
    assert handoff.gap_label(None) == handoff.gap_label("别的什么") == "—"
    window = {"start": "2026-11-01T00:00:00+08:00", "end": "2026-11-30T12:30:00+08:00"}
    assert handoff.window_text(window) == "2026-11-01 至 2026-11-30"
    assert handoff.window_text(None) == ""
    assert handoff.window_text({"start": "不是日期", "end": "2026-11-30"}) == "不是日期 至 2026-11-30"
