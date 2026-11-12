"""离线挖掘的运行器:抢锁、取数、跑图、查重、落库、清理旧数据。

管理面触发后在后台任务里跑,触发请求立即拿到 run_id。整件事属观测面(ADR-0005
fail-open):运行失败只是这一次没有主题清单与草稿,不影响在线对话;失败原因记在
运行行上给管理员看。运行只写自己的三张表(运行、主题、草稿),不向知识库写任何
内容——草稿要等人在管理面批准才发布。

同一时刻只允许一次运行(state/kb_ops.py 的 advisory lock),多副本部署也不会双跑。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from official_agent.config import get_effective_settings
from official_agent.evaluation.llm_common import TEMPERATURE_GENERATION, TEMPERATURE_SCORING
from official_agent.graphs.assistant import build_model
from official_agent.kb import query_log
from official_agent.kb import store as kb_store
from official_agent.kb.embedding import Embedder, EmbeddingError, embed_texts
from official_agent.kb_ops.cluster import MAX_PHRASINGS, ClusterPolicy, QueryRecord, Topic
from official_agent.kb_ops.draft import (
    KIND_ALT_QUESTION,
    KbReader,
    draft_prompt_versions,
    revise_draft,
    run_drafter,
)
from official_agent.kb_ops.drafting import DraftOutcome, DraftRoles, draft_topic, settle_drafts
from official_agent.kb_ops.graph import MiningDeps, MiningResult, TriageFn, run_mining
from official_agent.kb_ops.reader import StoreKbReader
from official_agent.kb_ops.triage import TriageVerdict, triage_prompt_version, triage_topic
from official_agent.kb_ops.verify import verify_draft, verify_prompt_version
from official_agent.state import kb_ops as kb_ops_state
from official_agent.tools import knowledge

logger = logging.getLogger(__name__)

#: 整次运行的墙钟预算。到点后不再发起新的模型调用,已有结果照常落库
MAX_WALL_SECONDS = 900
#: 一次运行最多处理这么多条没命中的检索(取最新的)。聚类最坏情况——每条都自成
#: 一簇——要做 n²/2 次点积,3000 条 1024 维向量约 25 秒 CPU
MAX_QUERIES = 3000
#: 会话数下限的下限:只有多人问过的问题才摘给管理员看,配置只能把它调高
_PRIVACY_MIN_THREADS = 2
_UNEXPECTED_ERROR = "运行失败,详情见服务日志"


class RunInProgressError(RuntimeError):
    """已有一次运行在进行中(管理面映射为 409)。"""


@dataclass(frozen=True)
class RunRequest:
    """一次运行的输入:时间窗与触发人。"""

    run_id: str
    window_start: datetime
    window_end: datetime
    triggered_by: int


# 后台运行任务的强引用集:事件循环只持弱引用,不留在集里的任务可能在完成前被回收
_background_tasks: set[asyncio.Task[None]] = set()


def cluster_policy(settings: Any) -> ClusterPolicy:
    """配置 → 聚类旋钮。会话数下限不低于隐私下限。"""
    return ClusterPolicy(
        similarity_threshold=float(settings.kb_ops_cluster_threshold),
        min_queries=int(settings.kb_ops_min_queries),
        min_threads=max(_PRIVACY_MIN_THREADS, int(settings.kb_ops_min_threads)),
        near_miss_floor=float(settings.kb_min_score) - float(settings.kb_ops_near_miss_margin),
    )


def _default_triage(settings: Any) -> TriageFn:
    """用配置里的强档模型做甄别;一次运行共用一个模型对象,第一次用到时才构造。

    构造放在调用里而不是这里:模型没配好时构造会抛错,那样每个主题各自记成 failed、
    清单照出,而不是整次运行失败。
    """
    model: Any = None

    async def triage(samples: Sequence[str], query_count: int, thread_count: int) -> Any:
        nonlocal model
        if model is None:
            model = build_model(settings, temperature=TEMPERATURE_SCORING)
        return await triage_topic(
            samples, query_count=query_count, thread_count=thread_count, model=model
        )

    return triage


async def start_run(*, days: int, triggered_by: int) -> str:
    """登记并在后台启动一次运行,返回 run_id。必须在事件循环里调用。

    时间窗是到当前时刻为止的最近 days 天。已有运行在跑时抛 RunInProgressError。
    """
    lock = await asyncio.to_thread(kb_ops_state.try_acquire_run_lock)
    if lock is None:
        raise RunInProgressError("已有一次知识库运营分析在进行中")
    try:
        # 读配置会查库(库内覆盖项),不在事件循环里做
        settings = await asyncio.to_thread(get_effective_settings)
        policy = cluster_policy(settings)
        window_end = datetime.now(UTC)
        request = RunRequest(
            run_id=f"kbops_{secrets.token_hex(6)}",
            window_start=window_end - timedelta(days=days),
            window_end=window_end,
            triggered_by=triggered_by,
        )
        await asyncio.to_thread(
            kb_ops_state.create_run,
            request.run_id,
            window_start=request.window_start,
            window_end=request.window_end,
            triggered_by=triggered_by,
            params={
                "days": days,
                "cluster_threshold": policy.similarity_threshold,
                "min_queries": policy.min_queries,
                "min_threads": policy.min_threads,
                "near_miss_floor": round(policy.near_miss_floor, 4),
                "embed_model": settings.embed_model,
            },
        )
    except Exception:
        await asyncio.to_thread(lock.close)
        raise

    async def _run_then_release() -> None:
        try:
            await execute_run(request, policy)
        finally:
            await asyncio.to_thread(lock.close)

    task = asyncio.create_task(_run_then_release())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return request.run_id


def _default_roles(settings: Any) -> DraftRoles:
    """起草、修订、审核三个调用的生产实现:强档模型,第一次用到时才构造。

    起草与修订用生成档温度,审核用评分档:前者要换词检索,后者要判断稳定。
    """
    models: dict[float, Any] = {}

    def model_at(temperature: float) -> Any:
        if temperature not in models:
            models[temperature] = build_model(settings, temperature=temperature)
        return models[temperature]

    async def draft(question: str, topic: str, reader: KbReader, deadline: float) -> Any:
        model = model_at(TEMPERATURE_GENERATION)
        return await run_drafter(question, topic, model=model, reader=reader, deadline=deadline)

    async def revise(proposal: Any, problems: Any, chunks: Any) -> Any:
        model = model_at(TEMPERATURE_GENERATION)
        return await revise_draft(proposal, problems, chunks, model=model)

    async def verify(proposal: Any, chunks: Any, neighbors: Any) -> Any:
        return await verify_draft(proposal, chunks, neighbors, model=model_at(TEMPERATURE_SCORING))

    return DraftRoles(draft=draft, revise=revise, verify=verify)


async def execute_run(
    request: RunRequest,
    policy: ClusterPolicy,
    *,
    embed: Embedder | None = None,
    triage: TriageFn | None = None,
    roles: DraftRoles | None = None,
    reader: KbReader | None = None,
) -> None:
    """跑一次已登记的运行并落库。不抛异常:失败记在运行行上。

    embed / triage / roles / reader 缺省走配置的嵌入端点、强档模型与知识库;测试
    注入假实现。起草由配置 kb_ops_drafting_enabled 控制,没打开时只出主题清单。
    """
    from official_agent.observability import langfuse_callbacks

    try:
        settings = await asyncio.to_thread(get_effective_settings)
        embed = embed or embed_texts
        deadline = time.monotonic() + MAX_WALL_SECONDS
        drafting = None
        if settings.kb_ops_drafting_enabled:
            roles = roles or _default_roles(settings)
            # 起草读知识库的口径与在线检索一致:常青条目加当前开放周期的条目
            reader = reader or StoreKbReader(await knowledge.open_cycle_ids())
            drafting = _drafting_with(reader, roles, deadline)
        deps = MiningDeps(
            embed=embed,
            triage=triage or _default_triage(settings),
            policy=policy,
            deadline=deadline,
            drafting=drafting,
            callbacks=list(langfuse_callbacks()),
            correlation_id=request.run_id,
        )
        window = await asyncio.to_thread(
            query_log.summarize_window,
            since=request.window_start,
            until=request.window_end,
            near_miss_floor=policy.near_miss_floor,
        )
        rows = await asyncio.to_thread(
            query_log.list_unanswered,
            since=request.window_start,
            until=request.window_end,
            limit=MAX_QUERIES,
        )
        records = [
            QueryRecord(
                id=int(r["id"]),
                thread_id=str(r["thread_id"]),
                query=str(r["query"]),
                best_vec_score=r["best_vec_score"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
        result = await run_mining(records, deps)
        drafts, deduped = await _settle(result, embed, policy.similarity_threshold)
        prompt_versions = {"triage": triage_prompt_version()}
        if drafting is not None:
            prompt_versions |= {**draft_prompt_versions(), "verify": verify_prompt_version()}
        await asyncio.to_thread(
            kb_ops_state.finish_run,
            request.run_id,
            stats=_run_stats(window, len(records), result, drafts, deduped=deduped),
            prompt_versions=prompt_versions,
            topics=_topic_rows(result, drafts),
            drafts=_draft_rows(result, drafts, prompt_versions),
            superseded_draft_ids=sorted({i for d in drafts.values() for i in d.supersedes}),
        )
    except EmbeddingError as exc:
        # 嵌入端点的错误文案是面向管理面的受控文案(未配置/连不上/维度不符),可以直接展示
        logger.warning("kb_ops 运行失败:嵌入不可用 run_id=%s error=%s", request.run_id, exc)
        await _record_failure(request.run_id, f"问题向量化失败:{exc}")
        return
    except Exception:  # noqa: BLE001
        # 观测面 fail-open(ADR-0005):这次没有清单,在线对话不受影响;原始异常可能带
        # SQL 或路径,只进日志,运行行上只留通用文案
        logger.warning("kb_ops 运行失败 run_id=%s", request.run_id, exc_info=True)
        await _record_failure(request.run_id, _UNEXPECTED_ERROR)
        return
    await _purge_old(
        keep_runs=max(1, int(settings.kb_ops_run_retention)),
        draft_days=max(1, int(settings.kb_ops_draft_retention_days)),
    )
    logger.info(
        "kb_ops 运行完成 run_id=%s queries=%s topics=%s drafts=%s",
        request.run_id,
        len(records),
        len(result.topics),
        sum(1 for d in drafts.values() if d.status == kb_ops_state.DRAFT_READY),
    )


def _drafting_with(reader: KbReader, roles: DraftRoles, deadline: float) -> Any:
    async def drafting(question: str, topic: str) -> DraftOutcome:
        return await draft_topic(question, topic, reader=reader, roles=roles, deadline=deadline)

    return drafting


async def _settle(
    result: MiningResult, embed: Embedder, threshold: float
) -> tuple[dict[int, DraftOutcome], bool]:
    """各主题的起草结局过一遍查重,返回 ({rank: 结局}, 查重是否做成了)。"""
    outcomes = {rank: t.draft for rank, t in result.triage.items() if t.draft is not None}
    if not any(o.status == kb_ops_state.DRAFT_READY for o in outcomes.values()):
        return outcomes, True
    try:
        faq_questions = await asyncio.to_thread(kb_store.list_faq_questions)
        pending = await asyncio.to_thread(kb_ops_state.list_pending_draft_questions)
        settled = await settle_drafts(
            outcomes,
            embed=embed,
            faq_questions=faq_questions,
            pending_drafts=pending,
            threshold=threshold,
        )
    except Exception:  # noqa: BLE001
        # 查重做不成(多半是嵌入端点中途不可用)不该丢掉已经审过的草稿:照常落库,
        # 可能与已有问法或旧草稿重复,由管理员在审核时处理;统计量里标出来
        logger.warning("kb_ops 草稿查重失败,本次草稿未去重", exc_info=True)
        return outcomes, False
    return settled, True


async def _record_failure(run_id: str, error: str) -> None:
    try:
        await asyncio.to_thread(kb_ops_state.fail_run, run_id, error)
    except Exception:  # noqa: BLE001
        # 连失败状态都写不进(多半是库不可用):这一行会停在 running,下一次运行开始时
        # 被改记为「进程中断」。这里只能留日志
        logger.warning("kb_ops 运行失败状态写入失败 run_id=%s", run_id, exc_info=True)


async def _purge_old(*, keep_runs: int, draft_days: int) -> None:
    try:
        await asyncio.to_thread(kb_ops_state.purge_old_runs, keep_runs)
        await asyncio.to_thread(kb_ops_state.purge_decided_drafts, draft_days)
    except Exception:  # noqa: BLE001
        # 清理失败不影响刚落库的清单;旧数据多留一轮,下一次运行再清
        logger.warning("kb_ops 旧运行清理失败(已忽略)", exc_info=True)


def _count(values: Sequence[str]) -> dict[str, int]:
    return {value: values.count(value) for value in sorted(set(values))}


def _run_stats(
    window: dict[str, int],
    query_count: int,
    result: MiningResult,
    drafts: dict[int, DraftOutcome],
    *,
    deduped: bool,
) -> dict[str, Any]:
    """运行行上的统计量。全部由代码数出来,模型不参与。"""
    statuses = [t.status for t in result.triage.values()]
    usages = [t.usage for t in result.triage.values() if t.usage]
    usages += [d.usage for d in drafts.values()]
    ready = [d for d in drafts.values() if d.status == kb_ops_state.DRAFT_READY and d.proposal]
    return {
        "window": window,
        "queries": query_count,
        # 时间窗里没命中的检索比实际处理的多,说明被 MAX_QUERIES 截掉了较早的一部分
        "queries_truncated": window.get("not_found", 0) > query_count,
        "unique_queries": result.unique_queries,
        "clusters": result.cluster_count,
        "below_support": result.below_support,
        "topics": len(result.topics),
        "triage": _count(statuses),
        "drafts": _count([d.status for d in drafts.values()]),
        "draft_kinds": _count([d.proposal.kind for d in ready if d.proposal]),
        "drafts_deduped": deduped,
        "budget_exhausted": kb_ops_state.TRIAGE_OUT_OF_BUDGET in statuses
        or any(d.status == kb_ops_state.DRAFT_OUT_OF_BUDGET for d in drafts.values()),
        "input_tokens": sum(u.get("input_tokens") or 0 for u in usages),
        "output_tokens": sum(u.get("output_tokens") or 0 for u in usages),
    }


def _topic_rows(
    result: MiningResult, drafts: dict[int, DraftOutcome]
) -> list[kb_ops_state.TopicRow]:
    rows = []
    for topic in result.topics:
        triage = result.triage[topic.rank]
        verdict = triage.verdict
        draft = drafts.get(topic.rank)
        rows.append(
            kb_ops_state.TopicRow(
                rank=topic.rank,
                query_count=topic.query_count,
                thread_count=topic.thread_count,
                near_miss_count=topic.near_miss_count,
                best_vec_score=topic.best_vec_score,
                first_seen=topic.first_seen,
                last_seen=topic.last_seen,
                sample_query_ids=tuple(p.query_id for p in topic.phrasings[:MAX_PHRASINGS]),
                triage_status=triage.status,
                scope=verdict.scope if verdict else None,
                topic=verdict.topic if verdict else None,
                canonical_question=verdict.canonical_question if verdict else None,
                reason=verdict.reason if verdict else None,
                draft_status=draft.status if draft else None,
            )
        )
    return rows


def _draft_rows(
    result: MiningResult, drafts: dict[int, DraftOutcome], prompt_versions: dict[str, str]
) -> list[kb_ops_state.DraftRow]:
    """要落库的草稿:定稿的(待审),以及被代码驳回的(留给人看它错在哪)。"""
    stored = (kb_ops_state.DRAFT_READY, kb_ops_state.DRAFT_AUTO_REJECTED)
    rows = []
    for topic in result.topics:
        outcome = drafts.get(topic.rank)
        verdict = result.triage[topic.rank].verdict
        if outcome is None or verdict is None or outcome.status not in stored:
            continue
        rows.append(_draft_row(topic, verdict, outcome, prompt_versions))
    return rows


def _draft_row(
    topic: Topic, verdict: TriageVerdict, outcome: DraftOutcome, prompt_versions: dict[str, str]
) -> kb_ops_state.DraftRow:
    proposal = outcome.proposal
    if proposal is None:
        raise ValueError("没有草稿内容的起草结局不落库")
    chunks = outcome.chunks
    evidence = [
        {
            "sentence": s.text,
            "chunk_id": s.chunk_id,
            "quote": s.quote,
            "source_id": chunks[s.chunk_id].source_id if s.chunk_id in chunks else "",
            "source_title": chunks[s.chunk_id].title if s.chunk_id in chunks else "",
        }
        for s in proposal.sentences
    ]
    # 证据来自绑定了周期的条目时,新条目跟着绑同一个周期:否则它会变成常青条目,
    # 明年仍被检到。跨了多个周期取最新的,并在 review 里标出来交给人定
    cycles = sorted({c.cycle_id for c in chunks.values() if c.cycle_id is not None})
    is_alt = proposal.kind == KIND_ALT_QUESTION
    accepted = outcome.status == kb_ops_state.DRAFT_READY
    return kb_ops_state.DraftRow(
        topic_rank=topic.rank,
        kind=proposal.kind,
        topic=verdict.topic,
        question=proposal.question,
        # 补问法沿用目标 FAQ 的答案;由新 FAQ 转成的补问法留着起草的答案供人对照
        answer="".join(s.text.strip() for s in proposal.sentences)
        if not is_alt or outcome.converted_from
        else "",
        evidence=evidence,
        target_source_id=proposal.target_source_id,
        conflict_with=tuple(dict.fromkeys(c.source_id for c in outcome.conflicts)),
        support={
            "query_count": topic.query_count,
            "thread_count": topic.thread_count,
            "near_miss_count": topic.near_miss_count,
            "sample_query_ids": [p.query_id for p in topic.phrasings[:MAX_PHRASINGS]],
        },
        review={
            "findings": [
                {"index": f.index, "verdict": f.verdict, "reason": f.reason_text}
                for f in outcome.findings
            ],
            "conflicts": [c.model_dump() for c in outcome.conflicts],
            "revised": outcome.revised,
            "downgraded": outcome.downgraded,
            "converted_from": outcome.converted_from,
            "mixed_cycles": len(cycles) > 1,
            "note": proposal.note,
        },
        prompt_versions=prompt_versions,
        cycle_id=None if is_alt or not cycles else cycles[-1],
        status=kb_ops_state.DRAFT_PENDING if accepted else kb_ops_state.DRAFT_REJECTED,
    )
