"""忠实度抽样的运行器:抢锁、抽样、逐轮核对、落库、清理旧运行。

管理面触发后在后台任务里跑。属观测面(ADR-0005 fail-open):运行失败只是这一次
没有忠实度数字,不影响在线对话;它只写自己的两张表。配置里没打开
(kb_ops_faithfulness_enabled)时拒绝启动。

预算由代码定:一次至多核对 MAX_SAMPLE_SIZE 轮,同时在跑的模型调用 CONCURRENCY 个,
单次调用与整次运行各有墙钟上限;到点后没轮到的样本记为预算用完,已有结果照常落库。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from official_agent.config import get_effective_settings
from official_agent.evaluation.llm_common import TEMPERATURE_SCORING
from official_agent.graphs.assistant import build_model
from official_agent.kb import store as kb_store
from official_agent.kb.store import ChunkRecord
from official_agent.kb_ops.draft import Usage
from official_agent.kb_ops.faithfulness import (
    ClaimFinding,
    FaithfulnessOutput,
    faithfulness_prompt_version,
    judge_turn,
    review_claims,
    score_turn,
)
from official_agent.kb_ops.runner import RunInProgressError
from official_agent.state import kb_faithfulness as state

logger = logging.getLogger(__name__)

#: 整次运行的墙钟预算
MAX_WALL_SECONDS = 900
#: 一次运行最多核对的对话轮数(管理面传入的 sample_size 不能超过它)
MAX_SAMPLE_SIZE = 100
#: 同时在跑的核对调用数。离线批处理不该挤占在线对话的模型额度
CONCURRENCY = 3
#: 单次核对调用的墙钟上限(含一次回灌重试)
CALL_TIMEOUT_SECONDS = 60.0
#: 给模型看的证据上限:块数与总字数。命中的条目很长时只取排在前面的块
_MAX_EVIDENCE_CHUNKS = 12
_MAX_EVIDENCE_CHARS = 8000
_UNEXPECTED_ERROR = "运行失败,详情见服务日志"

#: 核对函数:(问题, 回复, 证据块) → (逐条判断, token 用量)
JudgeFn = Callable[
    [str, str, Sequence[ChunkRecord]], Awaitable[tuple[FaithfulnessOutput, Usage]]
]
#: 证据读取:(命中的条目 id, 当时的周期过滤值) → 这些条目现在读得到的块
ChunkReader = Callable[[Sequence[str], Sequence[int]], Awaitable[list[ChunkRecord]]]


class FaithfulnessDisabledError(RuntimeError):
    """忠实度抽样没有在配置里打开(管理面映射为 409)。"""


@dataclass(frozen=True)
class RunRequest:
    """一次抽样运行的输入:时间窗、样本数与触发人。"""

    run_id: str
    window_start: datetime
    window_end: datetime
    sample_size: int
    triggered_by: int


@dataclass(frozen=True)
class _SampleResult:
    conversation_id: int
    status: str
    source_ids: tuple[str, ...]
    findings: tuple[ClaimFinding, ...] = ()
    usage: Usage | None = None


# 后台运行任务的强引用集:事件循环只持弱引用,不留在集里的任务可能在完成前被回收
_background_tasks: set[asyncio.Task[None]] = set()


def _default_judge(settings: Any) -> JudgeFn:
    """用配置里的强档模型核对;一次运行共用一个模型对象,第一次用到时才构造。"""
    model: Any = None

    async def judge(question: str, reply: str, chunks: Sequence[ChunkRecord]) -> Any:
        nonlocal model
        if model is None:
            model = build_model(settings, temperature=TEMPERATURE_SCORING)
        return await judge_turn(question, reply, chunks, model=model)

    return judge


async def read_evidence(source_ids: Sequence[str], cycle_ids: Sequence[int]) -> list[ChunkRecord]:
    """那一轮命中的条目现在读得到的块,按命中顺序,截到证据上限。

    读的是条目的当前内容:条目在回复之后被改过的话,核对的是回复与新内容。按当时的
    周期过滤值读,换届之后那一届的条目读不到,样本记为没有证据。
    """
    chunks: list[ChunkRecord] = []
    total_chars = 0
    for source_id in source_ids:
        for chunk in await asyncio.to_thread(
            kb_store.list_source_chunks, source_id, cycle_ids=cycle_ids
        ):
            total_chars += len(chunk.text)
            if len(chunks) >= _MAX_EVIDENCE_CHUNKS or total_chars > _MAX_EVIDENCE_CHARS:
                return chunks
            chunks.append(chunk)
    return chunks


async def start_run(*, days: int, sample_size: int, triggered_by: int) -> str:
    """登记并在后台启动一次抽样,返回 run_id。必须在事件循环里调用。

    没在配置里打开抛 FaithfulnessDisabledError;已有抽样在跑抛 RunInProgressError。
    """
    settings = await asyncio.to_thread(get_effective_settings)
    if not settings.kb_ops_faithfulness_enabled:
        raise FaithfulnessDisabledError("忠实度抽样未开启(配置项 kb_ops_faithfulness_enabled)")
    lock = await asyncio.to_thread(state.try_acquire_run_lock)
    if lock is None:
        raise RunInProgressError("已有一次忠实度抽样在进行中")
    try:
        window_end = datetime.now(UTC)
        request = RunRequest(
            run_id=f"kbfaith_{secrets.token_hex(6)}",
            window_start=window_end - timedelta(days=days),
            window_end=window_end,
            sample_size=min(sample_size, MAX_SAMPLE_SIZE),
            triggered_by=triggered_by,
        )
        await asyncio.to_thread(
            state.create_run,
            request.run_id,
            window_start=request.window_start,
            window_end=request.window_end,
            triggered_by=triggered_by,
            params={"days": days, "sample_size": request.sample_size},
        )
    except Exception:
        await asyncio.to_thread(lock.close)
        raise

    async def _run_then_release() -> None:
        try:
            await execute_run(request)
        finally:
            await asyncio.to_thread(lock.close)

    task = asyncio.create_task(_run_then_release())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return request.run_id


async def execute_run(
    request: RunRequest,
    *,
    judge: JudgeFn | None = None,
    read_chunks: ChunkReader | None = None,
) -> None:
    """跑一次已登记的抽样并落库。不抛异常:失败记在运行行上。

    judge / read_chunks 缺省走配置的强档模型与知识库;测试注入假实现。
    """
    try:
        settings = await asyncio.to_thread(get_effective_settings)
        judge = judge or _default_judge(settings)
        read_chunks = read_chunks or read_evidence
        deadline = time.monotonic() + MAX_WALL_SECONDS
        window = {"since": request.window_start, "until": request.window_end}
        candidates = await asyncio.to_thread(state.count_candidates, **window)
        turns = await asyncio.to_thread(
            state.sample_candidates, seed=request.run_id, limit=request.sample_size, **window
        )
        citation = await asyncio.to_thread(state.summarize_citation_verdicts, **window)
        gate = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(
            *(_judge_sample(turn, judge, read_chunks, gate, deadline) for turn in turns)
        )
        await asyncio.to_thread(
            state.finish_run,
            request.run_id,
            stats=_run_stats(candidates, results, citation),
            prompt_versions={"faithfulness": faithfulness_prompt_version()},
            samples=[_sample_row(r) for r in results],
        )
    except Exception:  # noqa: BLE001
        # 观测面 fail-open(ADR-0005):这次没有忠实度数字,在线对话不受影响;原始异常
        # 可能带 SQL 或连接串,只进日志,运行行上只留通用文案
        logger.warning("kb_faithfulness 运行失败 run_id=%s", request.run_id, exc_info=True)
        await _record_failure(request.run_id)
        return
    try:
        keep = max(1, int(settings.kb_ops_run_retention))
        await asyncio.to_thread(state.purge_old_runs, keep)
    except Exception:  # noqa: BLE001
        # 清理失败不影响刚落库的结果;旧运行多留一轮,下一次运行再清
        logger.warning("kb_faithfulness 旧运行清理失败(已忽略)", exc_info=True)
    logger.info(
        "kb_faithfulness 运行完成 run_id=%s candidates=%s sampled=%s",
        request.run_id,
        candidates,
        len(results),
    )


async def _record_failure(run_id: str) -> None:
    try:
        await asyncio.to_thread(state.fail_run, run_id, _UNEXPECTED_ERROR)
    except Exception:  # noqa: BLE001
        # 连失败状态都写不进(多半是库不可用):这一行停在 running,下一次运行开始时
        # 被改记为「进程中断」。这里只能留日志
        logger.warning("kb_faithfulness 失败状态写入失败 run_id=%s", run_id, exc_info=True)


async def _judge_sample(
    turn: dict[str, Any],
    judge: JudgeFn,
    read_chunks: ChunkReader,
    gate: asyncio.Semaphore,
    deadline: float,
) -> _SampleResult:
    """核对一轮。任何失败都落成一个结局,不让一个样本拖垮整次运行。"""
    conversation_id = int(turn["conversation_id"])
    source_ids = tuple(turn["source_ids"])

    def result(status: str, **kwargs: Any) -> _SampleResult:
        return _SampleResult(conversation_id, status, source_ids, **kwargs)

    async with gate:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result(state.SAMPLE_OUT_OF_BUDGET)
        try:
            chunks = await read_chunks(source_ids, turn["cycle_ids"])
            if not chunks:
                return result(state.SAMPLE_NO_EVIDENCE)
            output, usage = await asyncio.wait_for(
                judge(str(turn["user_message"]), str(turn["reply_summary"]), chunks),
                timeout=min(CALL_TIMEOUT_SECONDS, remaining),
            )
        except TimeoutError:
            # 被整次运行的截止时间截断的算预算用完;自己跑满单次上限的算失败
            out_of_budget = time.monotonic() >= deadline
            logger.warning("kb_faithfulness 核对超时 conversation_id=%s", conversation_id)
            return result(state.SAMPLE_OUT_OF_BUDGET if out_of_budget else state.SAMPLE_FAILED)
        except Exception:  # noqa: BLE001
            # 这个样本没有结论,不计入忠实度的分母;模型端点不可用时整次运行的样本
            # 都会落到这里,统计量里看得出来
            logger.warning(
                "kb_faithfulness 核对失败 conversation_id=%s", conversation_id, exc_info=True
            )
            return result(state.SAMPLE_FAILED)
    findings = review_claims(output, {c.chunk_id: c for c in chunks})
    return result(state.SAMPLE_JUDGED, findings=tuple(findings), usage=usage)


def _sample_row(result: _SampleResult) -> state.SampleRow:
    score = score_turn(result.findings)
    return state.SampleRow(
        conversation_id=result.conversation_id,
        status=result.status,
        source_ids=result.source_ids,
        claims_total=score.total,
        supported=score.supported,
        unsupported=score.unsupported,
        contradicted=score.contradicted,
        claims=tuple(f.as_dict() for f in result.findings),
    )


def _run_stats(
    candidates: int, results: Sequence[_SampleResult], citation: dict[str, int]
) -> dict[str, Any]:
    """运行行上的统计量,全部由代码数出来。

    faithfulness 是「有依据的说法数 / 核对过的说法数」;没有任何说法可核对时为 None,
    不写成 0 或 1。citation 是同一时间窗内在线引用核验的判定计数。
    """
    statuses = [r.status for r in results]
    scores = [score_turn(r.findings) for r in results if r.status == state.SAMPLE_JUDGED]
    claims = sum(s.total for s in scores)
    supported = sum(s.supported for s in scores)
    usages = [r.usage for r in results if r.usage]
    return {
        "candidates": candidates,
        "sampled": len(results),
        "samples": {status: statuses.count(status) for status in sorted(set(statuses))},
        "claims": claims,
        "supported": supported,
        "unsupported": sum(s.unsupported for s in scores),
        "contradicted": sum(s.contradicted for s in scores),
        "faithfulness": round(supported / claims, 4) if claims else None,
        "turns_with_claims": sum(1 for s in scores if s.total),
        "fully_supported_turns": sum(1 for s in scores if s.faithful),
        "turns_with_contradiction": sum(1 for s in scores if s.contradicted),
        "budget_exhausted": state.SAMPLE_OUT_OF_BUDGET in statuses,
        "citation": citation,
        "input_tokens": sum(u.get("input_tokens") or 0 for u in usages),
        "output_tokens": sum(u.get("output_tokens") or 0 for u in usages),
    }
