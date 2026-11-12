"""一个主题从起草到定稿:起草 → 代码预检 → 审核 → 至多打回一次 → 裁决;以及定稿后的查重。

模型调用经 DraftRoles 注入(测试与 eval 换成假实现),读知识库经 KbReader。裁决
规则全部在代码里(kb_ops/verify.py):

- 任何一句与原文矛盾 → 整份驳回;
- 有无出处句 → 打回起草者一次;
- 打回后仍有无出处句 → 降级为「待补充」,答案清空,交给人写。

这里不落库。流程的任何失败都落成一个结局,不抛给调用方。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from official_agent.kb.embedding import Embedder
from official_agent.kb.store import ChunkRecord, SearchHit, same_question
from official_agent.kb_ops.dedupe import KnownQuestion, most_similar, similar_refs
from official_agent.kb_ops.draft import (
    KIND_ALT_QUESTION,
    KIND_MISSING,
    KIND_NEW_FAQ,
    DraftBudgetError,
    DraftProposal,
    KbReader,
    Usage,
    add_usage,
)
from official_agent.kb_ops.verify import (
    RULING_ACCEPT,
    RULING_REJECT,
    ConflictNote,
    ReviewOutput,
    SentenceFinding,
    apply_review,
    precheck,
    problem_lines,
    ruling,
)
from official_agent.state.kb_ops import (
    DRAFT_AUTO_REJECTED,
    DRAFT_DUPLICATE,
    DRAFT_FAILED,
    DRAFT_OUT_OF_BUDGET,
    DRAFT_READY,
)

logger = logging.getLogger(__name__)

#: 审核与修订单次调用的墙钟上限(含一次回灌重试)
ROLE_CALL_TIMEOUT_SECONDS = 60.0
#: 给审核者看的「最相近的现有条目」数
_NEIGHBOR_COUNT = 3


@dataclass(frozen=True)
class DraftRoles:
    """起草流程里三个模型调用的口子。

    draft(标准问法, 主题名, reader, deadline) / revise(草稿, 逐句问题, 原文块) /
    verify(草稿, {chunk_id: 原文块}, 相近的现有条目),各返回 (结果, token 用量)。
    """

    draft: Callable[[str, str, KbReader, float], Awaitable[tuple[DraftProposal, Usage]]]
    revise: Callable[
        [DraftProposal, Sequence[str], Sequence[ChunkRecord]],
        Awaitable[tuple[DraftProposal, Usage]],
    ]
    verify: Callable[
        [DraftProposal, Mapping[str, ChunkRecord], Sequence[SearchHit]],
        Awaitable[tuple[ReviewOutput, Usage]],
    ]


@dataclass(frozen=True)
class DraftOutcome:
    """一个主题的起草结局。

    status=DRAFT_READY 时 proposal 是定稿(可能是降级后的「待补充」);
    DRAFT_AUTO_REJECTED 时 proposal 是被驳回的那一稿,留给人看它错在哪;
    其余结局没有 proposal。chunks 是定稿引用到的原文块(落证据用)。
    supersedes / duplicate_of 由查重填:要顶替的旧草稿 id、与之重复的已有问法归属。
    """

    status: str
    proposal: DraftProposal | None = None
    findings: tuple[SentenceFinding, ...] = ()
    conflicts: tuple[ConflictNote, ...] = ()
    chunks: Mapping[str, ChunkRecord] = field(default_factory=dict)
    revised: bool = False
    downgraded: bool = False
    converted_from: str = ""
    supersedes: tuple[int, ...] = ()
    usage: Usage = field(default_factory=dict)


class _OutOfBudgetError(Exception):
    """整次运行的墙钟预算用完。"""


async def _call_role(awaitable: Awaitable[Any], deadline: float) -> Any:
    """等一次审核或修订调用;被整次运行的截止时间截断抛 _OutOfBudgetError。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        # 协程没有被等待就丢弃会告警,显式关掉
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise _OutOfBudgetError
    try:
        return await asyncio.wait_for(
            awaitable, timeout=min(ROLE_CALL_TIMEOUT_SECONDS, remaining)
        )
    except TimeoutError:
        if time.monotonic() >= deadline:
            raise _OutOfBudgetError from None
        raise


async def draft_topic(
    question: str, topic: str, *, reader: KbReader, roles: DraftRoles, deadline: float
) -> DraftOutcome:
    """为一个主题产出草稿。question / topic 是缺口甄别给出的标准问法与主题名。"""
    usage: Usage = {}
    try:
        return await _draft_and_review(question, topic, reader, roles, deadline, usage)
    except (DraftBudgetError, _OutOfBudgetError):
        logger.warning("kb_ops 起草预算用完 topic=%s", topic)
        return DraftOutcome(status=DRAFT_OUT_OF_BUDGET, usage=usage)
    except Exception:  # noqa: BLE001
        # 观测面 fail-open(ADR-0005):这个主题没有草稿,主题清单上照常可见,
        # 管理员仍可以手工补条目
        logger.warning("kb_ops 起草失败 topic=%s", topic, exc_info=True)
        return DraftOutcome(status=DRAFT_FAILED, usage=usage)


async def _draft_and_review(
    question: str, topic: str, reader: KbReader, roles: DraftRoles, deadline: float, usage: Usage
) -> DraftOutcome:
    proposal, spent = await roles.draft(question, topic, reader, deadline)
    add_usage(usage, spent)
    if proposal.kind == KIND_MISSING:
        return DraftOutcome(status=DRAFT_READY, proposal=proposal, usage=usage)

    neighbors = (await reader.search(proposal.question))[:_NEIGHBOR_COUNT]
    revised = False
    while True:
        chunks = await reader.get_chunks([s.chunk_id for s in proposal.sentences])
        findings = precheck(proposal, chunks)
        conflicts: tuple[ConflictNote, ...] = ()
        if ruling(findings) == RULING_ACCEPT:
            # 代码查不出问题才值得花一次审核调用
            review, spent = await _call_role(roles.verify(proposal, chunks, neighbors), deadline)
            add_usage(usage, spent)
            findings = apply_review(proposal, chunks, review)
            conflicts = tuple(review.conflicts)
        decision = ruling(findings)
        outcome = DraftOutcome(
            status=DRAFT_READY,
            proposal=proposal,
            findings=tuple(findings),
            conflicts=conflicts,
            chunks=chunks,
            revised=revised,
            usage=usage,
        )
        if decision == RULING_ACCEPT:
            return outcome
        if decision == RULING_REJECT:
            return replace(outcome, status=DRAFT_AUTO_REJECTED)
        if revised:
            break
        revision, spent = await _call_role(
            roles.revise(proposal, problem_lines(findings), list(chunks.values())), deadline
        )
        add_usage(usage, spent)
        revised = True
        if revision.kind != proposal.kind:
            # 修订只许删句或换出处;改成「待补充」是允许的,改成别的类型不认
            break
        # 问题与补问法的目标条目不由修订决定
        proposal = revision.model_copy(
            update={"question": proposal.question, "target_source_id": proposal.target_source_id}
        )
    return DraftOutcome(
        status=DRAFT_READY,
        proposal=DraftProposal(kind=KIND_MISSING, question=proposal.question),
        findings=tuple(findings),
        revised=True,
        downgraded=True,
        usage=usage,
    )


async def settle_drafts(
    outcomes: Mapping[int, DraftOutcome],
    *,
    embed: Embedder,
    faq_questions: Sequence[tuple[str, str]],
    pending_drafts: Sequence[tuple[int, str]],
    threshold: float,
) -> dict[int, DraftOutcome]:
    """定稿后的查重,返回调整后的结局(键是主题的 rank,与传入的相同)。

    只处理 DRAFT_READY 的结局,按 rank 从小到大:
    - 新 FAQ 的问题与某条现有 FAQ 的某种问法是同一个问题 → 转成那条 FAQ 的补问法;
    - 补问法与目标 FAQ 已有的问法字面相同 → 记为重复,不产出草稿;
    - 与本次运行里排在前面的草稿是同一个问题 → 记为重复;
    - 与待审的旧草稿是同一个问题 → 新草稿顶替它们(supersedes)。
    faq_questions 是 (source_id, 问法),pending_drafts 是 (draft_id, 问题)。
    """
    ready = sorted(
        (rank, o) for rank, o in outcomes.items() if o.status == DRAFT_READY and o.proposal
    )
    if not ready:
        return dict(outcomes)
    texts = (
        [o.proposal.question for _, o in ready if o.proposal]
        + [text for _, text in faq_questions]
        + [text for _, text in pending_drafts]
    )
    vectors = await embed(texts)
    draft_vectors = vectors[: len(ready)]
    faq_vectors = vectors[len(ready) : len(ready) + len(faq_questions)]
    pending_vectors = vectors[len(ready) + len(faq_questions) :]
    known_faq = [
        KnownQuestion(ref=source_id, text=text, vector=vector)
        for (source_id, text), vector in zip(faq_questions, faq_vectors, strict=True)
    ]
    known_pending = [
        KnownQuestion(ref=str(draft_id), text=text, vector=vector)
        for (draft_id, text), vector in zip(pending_drafts, pending_vectors, strict=True)
    ]

    settled = dict(outcomes)
    accepted: list[KnownQuestion] = []
    for (rank, outcome), vector in zip(ready, draft_vectors, strict=True):
        proposal = outcome.proposal
        if proposal is None:
            continue
        if proposal.kind == KIND_NEW_FAQ:
            match = most_similar(vector, known_faq, threshold)
            if match is not None:
                proposal = proposal.model_copy(
                    update={"kind": KIND_ALT_QUESTION, "target_source_id": match.ref}
                )
                outcome = replace(outcome, proposal=proposal, converted_from=KIND_NEW_FAQ)
        is_known_phrasing = proposal.kind == KIND_ALT_QUESTION and any(
            source_id == proposal.target_source_id and same_question(text, proposal.question)
            for source_id, text in faq_questions
        )
        if is_known_phrasing or most_similar(vector, accepted, threshold) is not None:
            settled[rank] = replace(outcome, status=DRAFT_DUPLICATE)
            continue
        superseded = tuple(int(ref) for ref in similar_refs(vector, known_pending, threshold))
        settled[rank] = replace(outcome, supersedes=superseded)
        accepted.append(KnownQuestion(ref=str(rank), text=proposal.question, vector=vector))
    return settled
