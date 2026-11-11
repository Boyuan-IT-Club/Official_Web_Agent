"""把没答上的检索聚成主题:确定性聚类、支持度过滤、按影响面排序。纯函数,无 IO。

算法是「领头者聚类」:按 (created_at, id) 顺序逐个看问题,与已有各簇的领头问题比
余弦相似度,最像的那个达到阈值就归入,否则自己成为新簇的领头。簇中心固定为领头
问题的向量,不随成员加入而移动——移动的中心会让一串两两相近的问题把簇越拉越远,
而且结果依赖成员加入的先后。输入顺序固定,所以同样的输入必得同样的输出。

向量由调用方现算后传入,这里不落库、不调嵌入端点。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

#: 一个主题最多带出这么多种问法(给管理员看的样本、给甄别模型看的样本都从这里取)
MAX_PHRASINGS = 5


@dataclass(frozen=True)
class QueryRecord:
    """一次没答上的检索:kb_query_log 一行里聚类用得到的字段。query 已脱敏。"""

    id: int
    thread_id: str
    query: str
    best_vec_score: float | None
    created_at: datetime


@dataclass(frozen=True)
class ClusterPolicy:
    """聚类的四个旋钮。

    near_miss_floor 是「差一点命中」的下界:没命中的检索里,最高向量分不低于它的
    算差一点(上界是检索门槛本身,没命中的检索不会超过)。
    """

    similarity_threshold: float
    min_queries: int
    min_threads: int
    near_miss_floor: float


@dataclass(frozen=True)
class Phrasing:
    """主题下的一种问法。query_id 是这种问法最早那次检索的日志行 id。"""

    text: str
    count: int
    query_id: int


@dataclass(frozen=True)
class Topic:
    """一个达到最小支持度的主题。rank 从 1 起,越小影响面越大。

    phrasings 按出现次数从多到少排,至多 MAX_PHRASINGS 种;第一种可以当作这个主题
    在没有人工命名时的代表问法。
    """

    rank: int
    query_count: int
    thread_count: int
    near_miss_count: int
    best_vec_score: float | None
    first_seen: datetime
    last_seen: datetime
    phrasings: tuple[Phrasing, ...]


@dataclass(frozen=True)
class ClusterResult:
    """一次聚类的产出。below_support 是因为问的人太少而没进清单的簇数。"""

    topics: list[Topic]
    cluster_count: int
    below_support: int


def normalize_query(text: str) -> str:
    """去掉首尾与多余空白。字面相同的问题只算一种问法、只嵌入一次。"""
    return " ".join(text.split())


def is_near_miss(best_vec_score: float | None, near_miss_floor: float) -> bool:
    """这次没命中的检索是不是「差一点命中」。没有分数(库里没有任何候选)不算。"""
    return best_vec_score is not None and best_vec_score >= near_miss_floor


def gap_hint(near_miss_count: int, query_count: int) -> str:
    """主题更像哪一种缺口:'wording'(库里多半有,问法没对上)或 'missing'(库里多半没有)。

    一半以上的检索差一点命中就判 wording。这只是给人排优先级的提示:分数贴着门槛
    也可能是碰巧相近的无关内容,所以不据此自动做任何事。
    """
    if query_count > 0 and near_miss_count * 2 >= query_count:
        return "wording"
    return "missing"


def unit_vector(vector: Sequence[float]) -> list[float]:
    """归一化到单位长度,之后点积即余弦相似度。零向量原样返回(与任何向量相似度为 0)。"""
    norm = math.sqrt(math.sumprod(vector, vector))
    if norm == 0.0:
        return [float(x) for x in vector]
    return [float(x) / norm for x in vector]


def leader_clusters(vectors: Sequence[Sequence[float]], threshold: float) -> list[list[int]]:
    """领头者聚类。返回各簇的成员下标(输入顺序),每簇第一个是领头。

    vectors 必须已归一化。与多个领头都达到阈值时归入最相似的那个,相似度相同归入
    最早出现的簇。
    """
    leaders: list[Sequence[float]] = []
    clusters: list[list[int]] = []
    for index, vector in enumerate(vectors):
        best_cluster, best_similarity = -1, -1.0
        for cluster_index, leader in enumerate(leaders):
            similarity = math.sumprod(vector, leader)
            # 严格更高才换:相似度相同时留在最早出现的簇
            if similarity >= threshold and similarity > best_similarity:
                best_cluster, best_similarity = cluster_index, similarity
        if best_cluster < 0:
            leaders.append(vector)
            clusters.append([index])
        else:
            clusters[best_cluster].append(index)
    return clusters


def build_topics(
    records: Sequence[QueryRecord],
    vectors: Mapping[str, Sequence[float]],
    policy: ClusterPolicy,
) -> ClusterResult:
    """聚类、过滤、排序,一步到位。

    vectors 以 normalize_query 后的问题文本为键;每条记录的问题都必须在里面。
    排序先看来自多少个会话,再看被问了多少次:一个人反复问十次的影响面不如三个人
    各问一次。
    """
    ordered = sorted(records, key=lambda r: (r.created_at, r.id))
    by_text: dict[str, list[QueryRecord]] = {}
    for record in ordered:
        by_text.setdefault(normalize_query(record.query), []).append(record)
    texts = list(by_text)  # 按每种问法首次出现的先后
    units = [unit_vector(vectors[t]) for t in texts]
    clusters = leader_clusters(units, policy.similarity_threshold)

    supported: list[list[str]] = []
    for member_indexes in clusters:
        member_texts = [texts[i] for i in member_indexes]
        members = [r for t in member_texts for r in by_text[t]]
        if len(members) < policy.min_queries:
            continue
        if len({r.thread_id for r in members}) < policy.min_threads:
            continue
        supported.append(member_texts)

    unranked = [_summarize(member_texts, by_text, policy) for member_texts in supported]
    unranked.sort(key=lambda t: (-t.thread_count, -t.query_count, t.phrasings[0].query_id))
    topics = [replace(t, rank=rank) for rank, t in enumerate(unranked, start=1)]
    return ClusterResult(
        topics=topics, cluster_count=len(clusters), below_support=len(clusters) - len(supported)
    )


def _summarize(
    member_texts: Sequence[str],
    by_text: Mapping[str, Sequence[QueryRecord]],
    policy: ClusterPolicy,
) -> Topic:
    """一个簇的统计量与代表问法。rank 留 0,由调用方排序后填。"""
    members = [r for t in member_texts for r in by_text[t]]
    scores = [r.best_vec_score for r in members if r.best_vec_score is not None]
    phrasings = sorted(
        (Phrasing(text=t, count=len(by_text[t]), query_id=by_text[t][0].id) for t in member_texts),
        key=lambda p: (-p.count, p.query_id),
    )
    return Topic(
        rank=0,
        query_count=len(members),
        thread_count=len({r.thread_id for r in members}),
        near_miss_count=sum(
            1 for r in members if is_near_miss(r.best_vec_score, policy.near_miss_floor)
        ),
        best_vec_score=max(scores) if scores else None,
        first_seen=min(r.created_at for r in members),
        last_seen=max(r.created_at for r in members),
        phrasings=tuple(phrasings[:MAX_PHRASINGS]),
    )
