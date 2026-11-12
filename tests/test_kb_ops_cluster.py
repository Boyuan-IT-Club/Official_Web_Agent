"""知识库运营聚类的单测:纯函数,手写低维向量钉住每一条规则。"""

import random
from datetime import UTC, datetime, timedelta

import pytest

from official_agent.kb_ops.cluster import (
    MAX_PHRASINGS,
    ClusterPolicy,
    QueryRecord,
    build_topics,
    gap_hint,
    is_near_miss,
    leader_clusters,
    normalize_query,
    unit_vector,
)

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
_POLICY = ClusterPolicy(
    similarity_threshold=0.8, min_queries=3, min_threads=2, near_miss_floor=0.37
)

#: 手写向量:同字母的问法彼此相近,不同字母互相垂直
_VECTORS = {
    "会费A": [1.0, 0.0, 0.0],
    "会费B": [0.95, 0.31, 0.0],
    "会费C": [0.9, 0.0, 0.436],
    "面试A": [0.0, 1.0, 0.0],
    "面试B": [0.1, 0.99, 0.0],
    "食堂A": [0.0, 0.0, 1.0],
}


def _record(
    id_: int, text: str, thread: str, *, best: float | None = 0.1, minutes: int | None = None
) -> QueryRecord:
    """一条检索记录;时间缺省按 id 递增,保证 (created_at, id) 顺序就是 id 顺序。"""
    offset = id_ if minutes is None else minutes
    return QueryRecord(
        id=id_,
        thread_id=thread,
        query=text,
        best_vec_score=best,
        created_at=_T0 + timedelta(minutes=offset),
    )


# ── 小工具 ──────────────────────────────────────────────


def test_normalize_query_collapses_whitespace() -> None:
    assert normalize_query("  入社  要交钱吗\n") == "入社 要交钱吗"


def test_unit_vector_normalizes_and_tolerates_zero() -> None:
    assert unit_vector([3.0, 4.0]) == pytest.approx([0.6, 0.8])
    assert unit_vector([0.0, 0.0]) == [0.0, 0.0]


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0.44, True), (0.37, True), (0.3699, False), (0.1, False), (None, False)],
)
def test_is_near_miss(score: float | None, expected: bool) -> None:
    assert is_near_miss(score, 0.37) is expected


@pytest.mark.parametrize(
    ("near", "total", "expected"),
    [(3, 3, "wording"), (2, 4, "wording"), (1, 4, "missing"), (0, 3, "missing"), (0, 0, "missing")],
)
def test_gap_hint(near: int, total: int, expected: str) -> None:
    assert gap_hint(near, total) == expected


# ── 领头者聚类 ──────────────────────────────────────────


def _units(*names: str) -> list[list[float]]:
    return [unit_vector(_VECTORS[n]) for n in names]


def test_leader_clusters_groups_similar_and_separates_orthogonal() -> None:
    clusters = leader_clusters(_units("会费A", "面试A", "会费B", "面试B", "食堂A"), 0.8)
    assert clusters == [[0, 2], [1, 3], [4]]


def test_leader_clusters_compares_with_leader_not_with_latest_member() -> None:
    """簇中心固定为领头:c 离领头 a 不够近,即便它离后加入的 b 很近也不归入。"""
    a, b, c = [1.0, 0.0], unit_vector([0.85, 0.527]), unit_vector([0.5, 0.866])
    # a·b≈0.85 归入;b·c≈0.88 但 a·c=0.5:中心若随成员移动,c 会被一串相近的问题拉进来
    assert leader_clusters([a, b, c], 0.8) == [[0, 1], [2]]


def test_leader_clusters_picks_most_similar_leader() -> None:
    a, b = [1.0, 0.0], unit_vector([0.6, 0.8])
    near_b = unit_vector([0.75, 0.66])  # 与 a 0.75、与 b 0.98
    assert leader_clusters([a, b, near_b], 0.7) == [[0], [1, 2]]


def test_leader_clusters_tie_goes_to_earliest_cluster() -> None:
    a, b = [1.0, 0.0], [0.0, 1.0]
    between = unit_vector([1.0, 1.0])  # 与两个领头的相似度完全相同
    assert leader_clusters([a, b, between], 0.7) == [[0, 2], [1]]


def test_leader_clusters_threshold_is_inclusive() -> None:
    a, b = [1.0, 0.0], [0.6, 0.8]
    assert leader_clusters([a, b], 0.6) == [[0, 1]]
    assert leader_clusters([a, b], 0.61) == [[0], [1]]


def test_leader_clusters_zero_vector_stands_alone() -> None:
    assert leader_clusters([[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]], 0.5) == [[0], [1], [2]]


# ── 主题:支持度、统计量、排序 ───────────────────────────


def test_build_topics_merges_phrasings_and_counts_support() -> None:
    records = [
        _record(1, "会费A", "t1", best=0.42),
        _record(2, "会费B", "t2", best=0.40),
        _record(3, "会费A", "t3", best=0.41),
        _record(4, "会费C", "t1", best=0.20),
    ]
    result = build_topics(records, _VECTORS, _POLICY)
    assert result.cluster_count == 1 and result.below_support == 0
    (topic,) = result.topics
    assert topic.rank == 1
    assert (topic.query_count, topic.thread_count, topic.near_miss_count) == (4, 3, 3)
    assert topic.best_vec_score == pytest.approx(0.42)
    assert topic.first_seen == records[0].created_at
    assert topic.last_seen == records[3].created_at
    # 问法按出现次数从多到少;次数相同按最早那次检索的先后
    assert [(p.text, p.count, p.query_id) for p in topic.phrasings] == [
        ("会费A", 2, 1),
        ("会费B", 1, 2),
        ("会费C", 1, 4),
    ]


def test_build_topics_treats_whitespace_variants_as_one_phrasing() -> None:
    records = [
        _record(1, "会费A", "t1"),
        _record(2, " 会费A ", "t2"),
        _record(3, "会费A\n", "t3"),
    ]
    (topic,) = build_topics(records, _VECTORS, _POLICY).topics
    assert [(p.text, p.count) for p in topic.phrasings] == [("会费A", 3)]


def test_build_topics_drops_clusters_below_min_queries() -> None:
    records = [_record(1, "会费A", "t1"), _record(2, "会费B", "t2")]
    result = build_topics(records, _VECTORS, _POLICY)
    assert result.topics == [] and result.cluster_count == 1 and result.below_support == 1


def test_build_topics_drops_single_session_repeats() -> None:
    """一个人反复问出来的不算主题:这也是隐私下限,只有多人问过的才摘出来。"""
    records = [_record(i, "食堂A", "t1") for i in (1, 2, 3, 4)]
    result = build_topics(records, _VECTORS, _POLICY)
    assert result.topics == [] and result.below_support == 1


def test_build_topics_ranks_by_sessions_then_queries_then_age() -> None:
    records = [
        # 面试:2 个会话 5 次
        _record(1, "面试A", "t1"),
        _record(2, "面试A", "t1"),
        _record(3, "面试B", "t2"),
        _record(4, "面试A", "t2"),
        _record(5, "面试A", "t1"),
        # 会费:3 个会话 3 次——会话更多,排前面
        _record(6, "会费A", "t3"),
        _record(7, "会费B", "t4"),
        _record(8, "会费C", "t5"),
        # 食堂:2 个会话 3 次——会话数与面试相同,次数少,排最后
        _record(9, "食堂A", "t6"),
        _record(10, "食堂A", "t7"),
        _record(11, "食堂A", "t6"),
    ]
    topics = build_topics(records, _VECTORS, _POLICY).topics
    assert [(t.rank, t.phrasings[0].text) for t in topics] == [
        (1, "会费A"),
        (2, "面试A"),
        (3, "食堂A"),
    ]


def test_build_topics_equal_support_keeps_earlier_topic_first() -> None:
    records = [
        _record(1, "面试A", "t1"),
        _record(2, "会费A", "t3"),
        _record(3, "面试A", "t2"),
        _record(4, "会费A", "t4"),
        _record(5, "面试A", "t1"),
        _record(6, "会费A", "t3"),
    ]
    topics = build_topics(records, _VECTORS, _POLICY).topics
    assert [t.phrasings[0].text for t in topics] == ["面试A", "会费A"]


def test_build_topics_without_any_score_reports_none() -> None:
    records = [_record(i, "面试A", f"t{i}", best=None) for i in (1, 2, 3)]
    (topic,) = build_topics(records, _VECTORS, _POLICY).topics
    assert topic.best_vec_score is None and topic.near_miss_count == 0


def test_build_topics_caps_phrasings() -> None:
    vectors = {f"会费{i}": [1.0, 0.0, 0.001 * i] for i in range(MAX_PHRASINGS + 3)}
    records = [_record(i + 1, text, f"t{i}") for i, text in enumerate(vectors)]
    (topic,) = build_topics(records, vectors, _POLICY).topics
    assert topic.query_count == MAX_PHRASINGS + 3
    assert len(topic.phrasings) == MAX_PHRASINGS
    assert [p.query_id for p in topic.phrasings] == list(range(1, MAX_PHRASINGS + 1))


def test_build_topics_is_independent_of_input_order() -> None:
    records = [
        _record(1, "会费A", "t1", best=0.42),
        _record(2, "面试A", "t4"),
        _record(3, "会费B", "t2", best=0.40),
        _record(4, "面试B", "t5"),
        _record(5, "会费C", "t3"),
        _record(6, "面试A", "t4"),
        _record(7, "食堂A", "t6"),
    ]
    expected = build_topics(records, _VECTORS, _POLICY)
    shuffled = list(records)
    random.Random(7).shuffle(shuffled)
    assert build_topics(shuffled, _VECTORS, _POLICY) == expected
    assert build_topics(list(reversed(records)), _VECTORS, _POLICY) == expected


def test_build_topics_orders_by_time_before_id() -> None:
    """领头是时间上最早的问法,不是 id 最小的:后台写入的行 id 不保证与时间同序。"""
    # 边·中 0.85、中·远 0.88、边·远 0.5:只有「中」当领头,三条才聚得到一起
    vectors = {"边": [1.0, 0.0], "中": [0.85, 0.527], "远": [0.5, 0.866]}
    records = [
        _record(1, "边", "t1", minutes=30),
        _record(2, "中", "t2", minutes=10),
        _record(3, "远", "t3", minutes=20),
    ]
    result = build_topics(records, vectors, _POLICY)
    assert result.cluster_count == 1
    assert result.topics[0].query_count == 3

    # 对照:时间与 id 同序时「边」先到、成为领头,「远」离它太远,聚不成一个主题
    in_id_order = [_record(1, "边", "t1"), _record(2, "中", "t2"), _record(3, "远", "t3")]
    assert build_topics(in_id_order, vectors, _POLICY).cluster_count == 2


def test_build_topics_empty_input() -> None:
    result = build_topics([], {}, _POLICY)
    assert result.topics == [] and result.cluster_count == 0 and result.below_support == 0
