"""挖掘流水线图:聚类 → 逐主题甄别(并行扇出)。只做计算,不碰数据库。

取数与落库在 runner。这里的节点拿到的是已经读出来的检索记录,产出主题与各主题的
甄别结局。嵌入函数与甄别函数由调用方注入:图本身不知道模型和端点怎么配,测试
直接换成假实现。

预算:甄别是唯一花钱也是唯一慢的环节。每个主题开始前看一眼整次运行的截止时间,
过了就不再调模型,把这个主题记成「预算用完」——聚类结果照常产出,不判失败。
"""

from __future__ import annotations

import asyncio
import logging
import operator
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from official_agent.kb.embedding import Embedder
from official_agent.kb_ops.cluster import (
    ClusterPolicy,
    QueryRecord,
    Topic,
    build_topics,
    normalize_query,
)
from official_agent.kb_ops.triage import TriageVerdict, safe_samples
from official_agent.state.kb_ops import (
    TRIAGE_DONE,
    TRIAGE_FAILED,
    TRIAGE_NOT_SELECTED,
    TRIAGE_OUT_OF_BUDGET,
    TRIAGE_UNSAFE_SAMPLES,
)

logger = logging.getLogger(__name__)

#: 一次运行最多留下这么多个主题;排在后面的影响面小,列出来也没人看
MAX_TOPICS = 100
#: 其中最多这么多个交给模型甄别(成本闸),其余记为 not_selected
MAX_TRIAGE_TOPICS = 30
#: 同时在跑的甄别调用数。离线批处理不该挤占在线对话的模型额度
TRIAGE_CONCURRENCY = 3
#: 单次甄别调用的墙钟上限(含一次回灌重试)
TRIAGE_CALL_TIMEOUT_SECONDS = 60.0
#: 每批送给嵌入端点的问题数
_EMBED_BATCH_SIZE = 64

#: 甄别函数:(样本, 检索次数, 会话数) → (结论, token 用量)
TriageFn = Callable[
    [Sequence[str], int, int], Awaitable[tuple[TriageVerdict, dict[str, int | None] | None]]
]


@dataclass(frozen=True)
class TopicTriage:
    """一个主题的甄别结局。verdict 只在 status=done 时有值。"""

    rank: int
    status: str
    verdict: TriageVerdict | None = None
    usage: dict[str, int | None] | None = None


@dataclass(frozen=True)
class MiningResult:
    """一次挖掘的全部产出。triage 以主题的 rank 为键,每个主题都有一项。"""

    topics: list[Topic]
    triage: dict[int, TopicTriage]
    unique_queries: int
    cluster_count: int
    below_support: int


@dataclass(frozen=True)
class MiningDeps:
    """图运行所需的外部能力与预算。

    deadline 是 time.monotonic() 时刻。callbacks 与 correlation_id 只用于 trace:
    前者是观测回调,后者写进 trace 的 metadata,用来从一次运行找到它的模型调用。
    """

    embed: Embedder
    triage: TriageFn
    policy: ClusterPolicy
    deadline: float
    callbacks: list[Any] = field(default_factory=list)
    correlation_id: str = ""


class _MiningState(TypedDict, total=False):
    records: list[QueryRecord]
    topics: list[Topic]
    unique_queries: int
    cluster_count: int
    below_support: int
    # 各主题的甄别结局由并行分支各自追加
    triage: Annotated[list[TopicTriage], operator.add]


class _TriageTask(TypedDict):
    topic: Topic


async def _embed_in_batches(embed: Embedder, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH_SIZE):
        vectors.extend(await embed(texts[start : start + _EMBED_BATCH_SIZE]))
    return vectors


def _build_graph(deps: MiningDeps) -> Any:
    async def cluster(state: _MiningState) -> dict[str, Any]:
        records = state["records"]
        texts = list(dict.fromkeys(normalize_query(r.query) for r in records))
        vectors = await _embed_in_batches(deps.embed, texts)
        # 聚类是纯 CPU 计算,问题多时要算几秒到十几秒,放进线程池不占事件循环
        result = await asyncio.to_thread(
            build_topics, records, dict(zip(texts, vectors, strict=True)), deps.policy
        )
        return {
            "topics": result.topics[:MAX_TOPICS],
            "unique_queries": len(texts),
            "cluster_count": result.cluster_count,
            "below_support": result.below_support,
        }

    def fan_out(state: _MiningState) -> list[Send] | str:
        selected = state["topics"][:MAX_TRIAGE_TOPICS]
        if not selected:
            return END
        return [Send("triage", {"topic": topic}) for topic in selected]

    async def triage(task: _TriageTask) -> dict[str, Any]:
        return {"triage": [await _triage_one(task["topic"], deps)]}

    graph = StateGraph(_MiningState)
    graph.add_node("cluster", cluster)
    graph.add_node("triage", triage)
    graph.add_edge(START, "cluster")
    graph.add_conditional_edges("cluster", fan_out, ["triage", END])
    graph.add_edge("triage", END)
    return graph.compile()


async def _triage_one(topic: Topic, deps: MiningDeps) -> TopicTriage:
    """甄别一个主题。任何失败都落成一个结局,不让一个主题拖垮整次运行。"""
    samples = safe_samples([p.text for p in topic.phrasings])
    if not samples:
        return TopicTriage(rank=topic.rank, status=TRIAGE_UNSAFE_SAMPLES)
    remaining = deps.deadline - time.monotonic()
    if remaining <= 0:
        return TopicTriage(rank=topic.rank, status=TRIAGE_OUT_OF_BUDGET)
    try:
        verdict, usage = await asyncio.wait_for(
            deps.triage(samples, topic.query_count, topic.thread_count),
            timeout=min(TRIAGE_CALL_TIMEOUT_SECONDS, remaining),
        )
    except TimeoutError:
        # 被整次运行的截止时间截断的算预算用完;自己跑满单次上限的算失败
        status = TRIAGE_OUT_OF_BUDGET if time.monotonic() >= deps.deadline else TRIAGE_FAILED
        logger.warning("kb_ops 甄别超时 rank=%s status=%s", topic.rank, status)
        return TopicTriage(rank=topic.rank, status=status)
    except Exception:  # noqa: BLE001
        # 观测面 fail-open(ADR-0005):这个主题照常进清单,只是没有归类,管理员仍能
        # 看到它的热度与样本;模型端点不可用时整份清单退化为纯聚类结果
        logger.warning("kb_ops 甄别失败 rank=%s", topic.rank, exc_info=True)
        return TopicTriage(rank=topic.rank, status=TRIAGE_FAILED)
    return TopicTriage(rank=topic.rank, status=TRIAGE_DONE, verdict=verdict, usage=usage)


async def run_mining(records: Sequence[QueryRecord], deps: MiningDeps) -> MiningResult:
    """跑完整条流水线。嵌入失败会抛出(没有向量就没有清单);甄别失败不抛。"""
    if not records:
        return MiningResult(
            topics=[], triage={}, unique_queries=0, cluster_count=0, below_support=0
        )
    config: dict[str, Any] = {"max_concurrency": TRIAGE_CONCURRENCY}
    if deps.callbacks:
        config["callbacks"] = deps.callbacks
    if deps.correlation_id:
        config["metadata"] = {"correlation_id": deps.correlation_id}
    graph = _build_graph(deps)
    final: _MiningState = await graph.ainvoke({"records": list(records)}, config=config)
    topics = final.get("topics") or []
    triage = {t.rank: t for t in final.get("triage") or []}
    for topic in topics:
        triage.setdefault(topic.rank, TopicTriage(rank=topic.rank, status=TRIAGE_NOT_SELECTED))
    return MiningResult(
        topics=topics,
        triage=triage,
        unique_queries=int(final.get("unique_queries") or 0),
        cluster_count=int(final.get("cluster_count") or 0),
        below_support=int(final.get("below_support") or 0),
    )
