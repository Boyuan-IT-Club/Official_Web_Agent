"""一个主题从起草到定稿的编排,以及定稿后的查重。三个模型角色与知识库都用假实现。"""

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import pytest

from official_agent.kb.store import ChunkRecord, SearchHit
from official_agent.kb_ops import drafting
from official_agent.kb_ops.draft import DraftBudgetError, DraftProposal
from official_agent.kb_ops.drafting import DraftOutcome, DraftRoles, draft_topic, settle_drafts
from official_agent.kb_ops.verify import ConflictNote, ReviewOutput, SentenceVerdict

_FEE = ChunkRecord(
    chunk_id="kb_fee#0",
    source_id="kb_fee",
    title="会费说明",
    source_type="faq",
    text="Q: 加入社团需要交费吗?\nA: 本社团不向成员收取会费,日常活动经费由学院拨款和赞助支持。",
    heading_path="",
)
_GOOD = {
    "text": "加入社团不收取任何费用。",
    "chunk_id": "kb_fee#0",
    "quote": "本社团不向成员收取会费",
}
_PLANTED = {
    "text": "但要先交 500 元押金。",
    "chunk_id": "kb_fee#0",
    "quote": "日常活动经费由学院拨款",
}
_UNQUOTED = {"text": "入社后每周要参加例会。", "chunk_id": "kb_fee#0", "quote": "每周参加一次例会"}
_QUESTION = "加入社团需要交费吗?"


def _proposal(*sentences: dict, kind: str = "new_faq", **over: Any) -> DraftProposal:
    return DraftProposal(kind=kind, question=_QUESTION, sentences=list(sentences), **over)


def _supported(*quotes: str, conflicts: Sequence[ConflictNote] = ()) -> ReviewOutput:
    return ReviewOutput(
        sentences=[
            SentenceVerdict(index=i, verdict="supported", quote=q) for i, q in enumerate(quotes, 1)
        ],
        conflicts=list(conflicts),
    )


def _verdicts(*verdicts: str) -> ReviewOutput:
    return ReviewOutput(
        sentences=[
            SentenceVerdict(
                index=i, verdict=v, quote="本社团不向成员收取会费" if v == "supported" else ""
            )
            for i, v in enumerate(verdicts, 1)
        ]
    )


class _Reader:
    def __init__(self) -> None:
        self.searches: list[str] = []

    async def search(self, query: str) -> list[SearchHit]:
        self.searches.append(query)
        return [
            SearchHit(f"kb_n{i}", f"相近条目{i}", "内容", "", 0.5, chunk_id=f"kb_n{i}#0")
            for i in range(5)
        ]

    async def read_source(self, source_id: str) -> list[ChunkRecord]:
        return []

    async def get_chunks(self, chunk_ids: Sequence[str]) -> dict[str, ChunkRecord]:
        return {i: _FEE for i in chunk_ids if i == _FEE.chunk_id}


class _Script:
    """回放写死输出的三个角色,并记录各被调用了几次、收到了什么。"""

    def __init__(
        self,
        drafts: Any,
        *,
        reviews: Sequence[Any] = (),
        revisions: Sequence[Any] = (),
        usage: dict[str, int] | None = None,
    ) -> None:
        self._draft = drafts
        self._reviews = list(reviews)
        self._revisions = list(revisions)
        self._usage = usage or {"input_tokens": 100, "output_tokens": 10}
        self.verified: list[tuple[DraftProposal, dict, list]] = []
        self.revised: list[tuple[DraftProposal, list[str], list]] = []

    @staticmethod
    def _play(step: Any) -> Any:
        if isinstance(step, BaseException):
            raise step
        return step

    @property
    def roles(self) -> DraftRoles:
        async def draft(question: str, topic: str, reader: Any, deadline: float) -> Any:
            return self._play(self._draft), dict(self._usage)

        async def revise(proposal: DraftProposal, problems: Any, chunks: Any) -> Any:
            self.revised.append((proposal, list(problems), list(chunks)))
            return self._play(self._revisions[len(self.revised) - 1]), dict(self._usage)

        async def verify(proposal: DraftProposal, chunks: Any, neighbors: Any) -> Any:
            self.verified.append((proposal, dict(chunks), list(neighbors)))
            return self._play(self._reviews[len(self.verified) - 1]), dict(self._usage)

        return DraftRoles(draft=draft, revise=revise, verify=verify)


async def _run(script: _Script, *, seconds_left: float = 30.0) -> DraftOutcome:
    return await draft_topic(
        _QUESTION,
        "社团会费",
        reader=_Reader(),
        roles=script.roles,
        deadline=time.monotonic() + seconds_left,
    )


# ── 放行 ────────────────────────────────────────────────


async def test_grounded_draft_is_accepted_after_one_review() -> None:
    conflict = ConflictNote(source_id="kb_n1", reason="现有条目说要收费")
    script = _Script(
        _proposal(_GOOD), reviews=[_supported("本社团不向成员收取会费", conflicts=[conflict])]
    )
    outcome = await _run(script)

    assert outcome.status == "drafted" and outcome.proposal.kind == "new_faq"
    assert [(f.verdict, f.reason) for f in outcome.findings] == [("supported", "ok")]
    assert outcome.conflicts == (conflict,)
    assert outcome.chunks == {"kb_fee#0": _FEE}
    assert (outcome.revised, outcome.downgraded) == (False, False)
    assert outcome.usage == {"input_tokens": 200, "output_tokens": 20}  # 起草 + 审核
    # 审核者拿到的是代码取回的原文块,和最相近的 3 条现有条目
    (_, chunks, neighbors) = script.verified[0]
    assert chunks == {"kb_fee#0": _FEE}
    assert [n.source_id for n in neighbors] == ["kb_n0", "kb_n1", "kb_n2"]
    assert script.revised == []


async def test_missing_draft_skips_review() -> None:
    script = _Script(_proposal(kind="missing", note="查过会费、收费,没有"))
    outcome = await _run(script)
    assert outcome.status == "drafted" and outcome.proposal.kind == "missing"
    assert script.verified == [] and outcome.downgraded is False


# ── 打回一次 ────────────────────────────────────────────


async def test_code_detected_problem_goes_straight_to_revision_without_a_review_call() -> None:
    script = _Script(
        _proposal(_GOOD, _PLANTED),
        revisions=[_proposal(_GOOD)],
        reviews=[_supported("本社团不向成员收取会费")],
    )
    outcome = await _run(script)

    assert outcome.status == "drafted" and outcome.revised is True
    assert [s.text for s in outcome.proposal.sentences] == ["加入社团不收取任何费用。"]
    # 代码已经查出问题,不值得先花一次审核调用;修订之后才审
    assert len(script.verified) == 1 and len(script.revised) == 1
    (_, problems, chunks) = script.revised[0]
    assert problems == ["第 1 句:通过", "第 2 句:句子里的数字在原文里找不到"]
    assert chunks == [_FEE]


async def test_reviewer_unsupported_sends_back_once_then_accepts() -> None:
    script = _Script(
        _proposal(_GOOD, {**_GOOD, "text": "入社后可以领取纪念品。"}),
        reviews=[_verdicts("supported", "unsupported"), _supported("本社团不向成员收取会费")],
        revisions=[_proposal(_GOOD)],
    )
    outcome = await _run(script)
    assert outcome.status == "drafted" and outcome.revised and not outcome.downgraded
    assert len(script.verified) == 2 and len(script.revised) == 1
    assert script.revised[0][1] == ["第 1 句:通过", "第 2 句:审核者判定原文里没有这件事"]
    assert outcome.usage["input_tokens"] == 400  # 起草 + 审核 + 修订 + 再审


async def test_still_unsupported_after_revision_is_downgraded_to_missing() -> None:
    script = _Script(_proposal(_UNQUOTED), revisions=[_proposal(_UNQUOTED)])
    outcome = await _run(script)
    assert outcome.status == "drafted" and outcome.downgraded and outcome.revised
    assert outcome.proposal == DraftProposal(kind="missing", question=_QUESTION)
    assert outcome.proposal.answer == "" and outcome.chunks == {}  # 答案清空,交给人写
    assert outcome.findings[0].reason == "drafter_quote_missing"  # 留着原因给管理员看
    assert len(script.revised) == 1  # 只打回一次


async def test_reviewer_still_unhappy_after_revision_is_downgraded() -> None:
    script = _Script(
        _proposal(_GOOD),
        reviews=[_verdicts("unsupported"), _verdicts("unsupported")],
        revisions=[_proposal(_GOOD)],
    )
    outcome = await _run(script)
    assert outcome.proposal.kind == "missing" and outcome.downgraded
    assert len(script.verified) == 2 and len(script.revised) == 1


async def test_revision_to_missing_counts_as_downgrade() -> None:
    script = _Script(_proposal(_UNQUOTED), revisions=[_proposal(kind="missing", note="删完了")])
    outcome = await _run(script)
    assert outcome.proposal.kind == "missing" and outcome.downgraded and script.verified == []


async def test_revision_cannot_change_kind_question_or_target() -> None:
    switched = _proposal(_GOOD, kind="alt_question", target_source_id="kb_fee")
    outcome = await _run(_Script(_proposal(_UNQUOTED), revisions=[switched]))
    assert outcome.proposal.kind == "missing"  # 换类型不认,按没改好处理

    renamed = DraftProposal(kind="new_faq", question="入社要交 500 元对吗?", sentences=[_GOOD])
    script = _Script(
        _proposal(_UNQUOTED), revisions=[renamed], reviews=[_supported("本社团不向成员收取会费")]
    )
    outcome = await _run(script)
    assert outcome.status == "drafted" and outcome.proposal.question == _QUESTION

    alt = _proposal(_UNQUOTED, kind="alt_question", target_source_id="kb_fee")
    retargeted = _proposal(_GOOD, kind="alt_question", target_source_id="kb_other")
    script = _Script(alt, revisions=[retargeted], reviews=[_supported("本社团不向成员收取会费")])
    outcome = await _run(script)
    assert outcome.proposal.target_source_id == "kb_fee"


# ── 驳回 ────────────────────────────────────────────────


async def test_contradiction_rejects_without_a_second_chance() -> None:
    script = _Script(_proposal(_GOOD, _GOOD), reviews=[_verdicts("supported", "contradicted")])
    outcome = await _run(script)
    assert outcome.status == "auto_rejected" and script.revised == []
    assert outcome.proposal.kind == "new_faq"  # 被驳回的那一稿留着,给人看它错在哪
    assert [f.verdict for f in outcome.findings] == ["supported", "contradicted"]


async def test_contradiction_found_after_revision_also_rejects() -> None:
    script = _Script(
        _proposal(_GOOD),
        reviews=[_verdicts("unsupported"), _verdicts("contradicted")],
        revisions=[_proposal(_GOOD)],
    )
    outcome = await _run(script)
    assert outcome.status == "auto_rejected" and outcome.revised is True


# ── 失败与预算 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "script",
    [
        _Script(RuntimeError("模型端点 500")),
        _Script(ValueError("起草输出两次不合规")),
        _Script(_proposal(_GOOD), reviews=[ValueError("审核输出两次均不合规")]),
        _Script(_proposal(_UNQUOTED), revisions=[RuntimeError("模型端点 500")]),
    ],
)
async def test_role_failures_become_a_failed_outcome(script: _Script) -> None:
    outcome = await _run(script)
    assert outcome.status == "failed" and outcome.proposal is None


async def test_draft_budget_exhaustion_is_out_of_budget() -> None:
    outcome = await _run(_Script(DraftBudgetError("起草轮数 6 用完")))
    assert outcome.status == "out_of_budget" and outcome.proposal is None


async def test_no_review_call_after_the_run_deadline() -> None:
    script = _Script(_proposal(_GOOD), reviews=[_supported("本社团不向成员收取会费")])
    outcome = await _run(script, seconds_left=-1.0)
    assert outcome.status == "out_of_budget" and script.verified == []
    assert outcome.usage == {"input_tokens": 100, "output_tokens": 10}  # 起草花掉的仍然记账


async def test_review_cut_short_by_the_deadline_is_out_of_budget() -> None:
    script = _Script(_proposal(_GOOD))

    async def slow_verify(proposal: Any, chunks: Any, neighbors: Any) -> Any:
        await asyncio.sleep(5)

    roles = DraftRoles(draft=script.roles.draft, revise=script.roles.revise, verify=slow_verify)
    started = time.monotonic()
    outcome = await draft_topic(
        _QUESTION, "社团会费", reader=_Reader(), roles=roles, deadline=time.monotonic() + 0.05
    )
    assert outcome.status == "out_of_budget" and time.monotonic() - started < 2


async def test_review_exceeding_its_own_timeout_is_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(drafting, "ROLE_CALL_TIMEOUT_SECONDS", 0.05)
    script = _Script(_proposal(_GOOD))

    async def slow_verify(proposal: Any, chunks: Any, neighbors: Any) -> Any:
        await asyncio.sleep(5)

    roles = DraftRoles(draft=script.roles.draft, revise=script.roles.revise, verify=slow_verify)
    outcome = await draft_topic(
        _QUESTION, "社团会费", reader=_Reader(), roles=roles, deadline=time.monotonic() + 30
    )
    assert outcome.status == "failed"


# ── 查重 ────────────────────────────────────────────────

_AXES = {
    "加入社团需要交费吗?": 0,
    "入社要不要交钱": 0,
    "入社要钱吗": 0,
    "面试在哪里": 1,
    "技术部面试地点": 1,
    "社团有奖学金吗": 2,
}


async def _embed(texts: list[str]) -> list[list[float]]:
    # 夹具里的问法按去掉空白后的字面查坐标轴
    return [[1.0 if i == _AXES["".join(t.split())] else 0.0 for i in range(3)] for t in texts]


def _ready(question: str, kind: str = "new_faq", **over: Any) -> DraftOutcome:
    sentences = [] if kind == "missing" else [_GOOD]
    return DraftOutcome(
        status="drafted",
        proposal=DraftProposal(kind=kind, question=question, sentences=sentences, **over),
    )


async def _settle(outcomes: dict[int, DraftOutcome], *, faq=(), pending=()) -> dict:
    return await settle_drafts(
        outcomes, embed=_embed, faq_questions=list(faq), pending_drafts=list(pending), threshold=0.8
    )


async def test_new_faq_matching_an_existing_phrasing_becomes_an_alt_question() -> None:
    settled = await _settle(
        {1: _ready("入社要不要交钱")}, faq=[("kb_interview", "面试在哪里"), ("kb_fee", _QUESTION)]
    )
    outcome = settled[1]
    assert outcome.status == "drafted"
    assert outcome.proposal.kind == "alt_question" and outcome.proposal.target_source_id == "kb_fee"
    assert outcome.converted_from == "new_faq"
    assert outcome.proposal.sentences == _ready("x").proposal.sentences  # 证据留着给人对照


async def test_new_faq_without_a_similar_existing_question_stays() -> None:
    settled = await _settle({1: _ready("社团有奖学金吗")}, faq=[("kb_fee", _QUESTION)])
    assert settled[1].proposal.kind == "new_faq" and settled[1].converted_from == ""


async def test_missing_draft_is_never_converted() -> None:
    """「待补充」没有经过审核的答案:不能凭问题相似就挂到别的条目上。"""
    settled = await _settle({1: _ready("入社要不要交钱", "missing")}, faq=[("kb_fee", _QUESTION)])
    assert settled[1].proposal.kind == "missing"


async def test_alt_question_already_known_to_its_target_is_a_duplicate() -> None:
    alt = _ready("入社 要不要 交钱", "alt_question", target_source_id="kb_fee")
    settled = await _settle({1: alt}, faq=[("kb_fee", _QUESTION), ("kb_fee", "入社要不要交钱")])
    assert settled[1].status == "duplicate"
    # 同样的字面出现在别的条目上不算:补问法是挂在目标条目上的
    other = await _settle({1: alt}, faq=[("kb_other", "入社要不要交钱")])
    assert other[1].status == "drafted"


async def test_two_topics_asking_the_same_thing_keep_only_the_higher_ranked_draft() -> None:
    settled = await _settle(
        {2: _ready("入社要钱吗"), 1: _ready("入社要不要交钱"), 3: _ready("面试在哪里")}
    )
    assert {rank: o.status for rank, o in settled.items()} == {
        1: "drafted",
        2: "duplicate",
        3: "drafted",
    }


async def test_new_draft_supersedes_similar_pending_drafts() -> None:
    settled = await _settle(
        {1: _ready("入社要不要交钱"), 2: _ready("面试在哪里")},
        pending=[(7, "入社要钱吗"), (8, "社团有奖学金吗"), (9, _QUESTION)],
    )
    assert settled[1].supersedes == (7, 9) and settled[2].supersedes == ()


async def test_settle_ignores_outcomes_without_a_final_draft() -> None:
    rejected = DraftOutcome(status="auto_rejected", proposal=_proposal(_GOOD))
    failed = DraftOutcome(status="failed")

    async def never(_texts: list[str]) -> list[list[float]]:
        raise AssertionError("没有定稿时不该调嵌入")

    settled = await settle_drafts(
        {1: rejected, 2: failed}, embed=never, faq_questions=[], pending_drafts=[], threshold=0.8
    )
    assert settled == {1: rejected, 2: failed}


async def test_settle_embeds_everything_in_one_call() -> None:
    calls: list[list[str]] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return await _embed(texts)

    await settle_drafts(
        {1: _ready("入社要不要交钱")},
        embed=embed,
        faq_questions=[("kb_interview", "面试在哪里")],
        pending_drafts=[(7, "社团有奖学金吗")],
        threshold=0.8,
    )
    assert calls == [["入社要不要交钱", "面试在哪里", "社团有奖学金吗"]]
