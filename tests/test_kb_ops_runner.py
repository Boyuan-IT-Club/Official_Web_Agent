"""知识库运营运行器的单测:取数与落库打桩,验证编排、统计量与失败语义。

真库往返(锁、事务、清理)在 test_kb_ops_integration.py。
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from official_agent.kb.embedding import EmbeddingNotConfiguredError
from official_agent.kb_ops import runner
from official_agent.kb_ops.cluster import ClusterPolicy
from official_agent.kb_ops.runner import RunInProgressError, RunRequest
from official_agent.kb_ops.triage import TriageVerdict
from official_agent.state import kb_ops as kb_ops_state

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
_POLICY = ClusterPolicy(
    similarity_threshold=0.8, min_queries=2, min_threads=2, near_miss_floor=0.37
)
_REQUEST = RunRequest(
    run_id="kbops_test01", window_start=_T0 - timedelta(days=7), window_end=_T0, triggered_by=2
)
_WINDOW = {"total": 20, "ok": 12, "not_found": 6, "unavailable": 2, "near_miss": 3}


def _row(id_: int, thread: str, query: str, best: float | None = 0.1) -> dict[str, Any]:
    return {
        "id": id_,
        "thread_id": thread,
        "query": query,
        "best_vec_score": best,
        "created_at": _T0 - timedelta(hours=100 - id_),
    }


_ROWS = [
    _row(1, "t1", "入社要交钱吗", 0.42),
    _row(2, "t2", "社团收费吗", 0.40),
    _row(3, "t3", "入社要交钱吗", 0.10),
    _row(4, "t4", "面试在哪里"),
    _row(5, "t5", "面试在哪里"),
    _row(6, "t6", "你好"),
]
_AXES = {"入社要交钱吗": 0, "社团收费吗": 0, "面试在哪里": 1, "你好": 2}


async def _embed(texts: list[str]) -> list[list[float]]:
    return [[1.0 if i == _AXES[t] else 0.0 for i in range(3)] for t in texts]


async def _triage(samples, query_count, thread_count):
    if samples[0] == "面试在哪里":
        raise RuntimeError("模型端点 500")
    verdict = TriageVerdict(
        topic="社团会费",
        canonical_question="加入社团需要交费吗?",
        scope="official_knowledge",
        reason="公开规则",
    )
    return verdict, {"input_tokens": 300, "output_tokens": 40}


class _Store:
    """state/kb_ops 与 kb/query_log 的替身:记录调用,不连库。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[dict[str, Any]],
        *,
        drafting: bool = False,
    ) -> None:
        self.finished: dict[str, Any] | None = None
        self.failed: tuple[str, str] | None = None
        self.purged: list[int] = []
        self.purged_draft_days: list[int] = []
        self.window_args: dict[str, Any] = {}
        self.list_args: dict[str, Any] = {}

        def summarize_window(**kwargs):
            self.window_args = kwargs
            return dict(_WINDOW)

        def list_unanswered(**kwargs):
            self.list_args = kwargs
            return rows

        def finish_run(run_id, **kwargs):
            self.finished = {"run_id": run_id, **kwargs}

        def fail_run(run_id, error):
            self.failed = (run_id, error)

        def purge_old_runs(keep):
            self.purged.append(keep)
            return 0

        def purge_decided_drafts(days):
            self.purged_draft_days.append(days)
            return 0

        monkeypatch.setattr(runner.query_log, "summarize_window", summarize_window)
        monkeypatch.setattr(runner.query_log, "list_unanswered", list_unanswered)
        monkeypatch.setattr(runner.kb_ops_state, "finish_run", finish_run)
        monkeypatch.setattr(runner.kb_ops_state, "fail_run", fail_run)
        monkeypatch.setattr(runner.kb_ops_state, "purge_old_runs", purge_old_runs)
        monkeypatch.setattr(runner.kb_ops_state, "purge_decided_drafts", purge_decided_drafts)
        monkeypatch.setattr(
            runner,
            "get_effective_settings",
            lambda: SimpleNamespace(
                kb_ops_run_retention=8,
                kb_ops_draft_retention_days=365,
                kb_ops_drafting_enabled=drafting,
            ),
        )
        monkeypatch.setattr("official_agent.observability.langfuse_callbacks", lambda: [])


# ── 配置 → 聚类旋钮 ─────────────────────────────────────


def _settings(**over) -> SimpleNamespace:
    base = {
        "kb_ops_cluster_threshold": 0.75,
        "kb_ops_min_queries": 3,
        "kb_ops_min_threads": 2,
        "kb_ops_near_miss_margin": 0.08,
        "kb_min_score": 0.45,
    }
    return SimpleNamespace(**{**base, **over})


def test_cluster_policy_reads_settings() -> None:
    policy = runner.cluster_policy(_settings(kb_ops_min_threads=4))
    assert policy.similarity_threshold == 0.75
    assert (policy.min_queries, policy.min_threads) == (3, 4)
    assert policy.near_miss_floor == pytest.approx(0.37)


@pytest.mark.parametrize("configured", [0, 1])
def test_cluster_policy_never_goes_below_the_privacy_floor(configured: int) -> None:
    """会话数下限配成 1 也不生效:只有多人问过的问题才摘给管理员看。"""
    assert runner.cluster_policy(_settings(kb_ops_min_threads=configured)).min_threads == 2


# ── 一次运行 ────────────────────────────────────────────


async def test_execute_run_persists_topics_stats_and_prompt_version(monkeypatch) -> None:
    store = _Store(monkeypatch, _ROWS)
    await runner.execute_run(_REQUEST, _POLICY, embed=_embed, triage=_triage)

    assert store.window_args == {
        "since": _REQUEST.window_start,
        "until": _REQUEST.window_end,
        "near_miss_floor": 0.37,
    }
    assert store.list_args == {
        "since": _REQUEST.window_start,
        "until": _REQUEST.window_end,
        "limit": runner.MAX_QUERIES,
    }
    assert store.failed is None
    finished = store.finished
    assert finished is not None and finished["run_id"] == "kbops_test01"
    assert finished["prompt_versions"] == {"triage": "kb_ops_triage/v1"}
    assert finished["stats"] == {
        "window": _WINDOW,
        "queries": 6,
        "queries_truncated": False,
        "unique_queries": 4,
        "clusters": 3,
        "below_support": 1,
        "topics": 2,
        "triage": {"done": 1, "failed": 1},
        "drafts": {},
        "draft_kinds": {},
        "drafts_deduped": True,
        "budget_exhausted": False,
        "input_tokens": 300,
        "output_tokens": 40,
    }
    assert finished["drafts"] == [] and finished["superseded_draft_ids"] == []
    fee, interview = finished["topics"]
    assert fee == kb_ops_state.TopicRow(
        rank=1,
        query_count=3,
        thread_count=3,
        near_miss_count=2,
        best_vec_score=0.42,
        first_seen=_ROWS[0]["created_at"],
        last_seen=_ROWS[2]["created_at"],
        sample_query_ids=(1, 2),
        triage_status="done",
        scope="official_knowledge",
        topic="社团会费",
        canonical_question="加入社团需要交费吗?",
        reason="公开规则",
    )
    # 甄别失败的主题照常落库,只是没有归类
    assert (interview.rank, interview.triage_status, interview.scope) == (2, "failed", None)
    assert interview.topic is None and interview.sample_query_ids == (4,)
    assert fee.draft_status is None  # 起草关着:只出主题清单
    assert store.purged == [8] and store.purged_draft_days == [365]


async def test_execute_run_with_no_unanswered_queries_succeeds_empty(monkeypatch) -> None:
    store = _Store(monkeypatch, [])

    async def never(*_a, **_k):
        raise AssertionError("没有数据时不该调嵌入或模型")

    await runner.execute_run(_REQUEST, _POLICY, embed=never, triage=never)
    assert store.failed is None and store.finished is not None
    assert store.finished["topics"] == []
    assert store.finished["stats"]["topics"] == 0 and store.finished["stats"]["triage"] == {}


async def test_execute_run_flags_truncated_window(monkeypatch) -> None:
    """时间窗里没命中的检索比处理的多:统计量里要说出来。"""
    store = _Store(monkeypatch, _ROWS[:3])
    await runner.execute_run(_REQUEST, _POLICY, embed=_embed, triage=_triage)
    assert store.finished["stats"]["queries"] == 3
    assert store.finished["stats"]["queries_truncated"] is True


async def test_embedding_failure_is_recorded_with_actionable_message(monkeypatch, caplog) -> None:
    store = _Store(monkeypatch, _ROWS)

    async def embed(_texts):
        raise EmbeddingNotConfiguredError("embedding 未配置:需在 .env 设 EMBED_BASE_URL")

    with caplog.at_level(logging.WARNING, logger="official_agent.kb_ops.runner"):
        await runner.execute_run(_REQUEST, _POLICY, embed=embed, triage=_triage)  # 不抛

    assert store.finished is None
    assert store.failed == (
        "kbops_test01",
        "问题向量化失败:embedding 未配置:需在 .env 设 EMBED_BASE_URL",
    )
    assert store.purged == []  # 失败的运行不触发清理
    assert "嵌入不可用" in caplog.text


async def test_unexpected_failure_does_not_leak_internals(monkeypatch, caplog) -> None:
    store = _Store(monkeypatch, _ROWS)

    def boom(**_kwargs):
        raise RuntimeError("connection to postgresql://postgres:secret@db failed")

    monkeypatch.setattr(runner.query_log, "list_unanswered", boom)
    with caplog.at_level(logging.WARNING, logger="official_agent.kb_ops.runner"):
        await runner.execute_run(_REQUEST, _POLICY, embed=_embed, triage=_triage)

    assert store.failed == ("kbops_test01", "运行失败,详情见服务日志")
    assert "secret" not in store.failed[1]
    assert "kb_ops 运行失败 run_id=kbops_test01" in caplog.text  # 原始异常在日志里


async def test_failure_to_record_failure_is_swallowed(monkeypatch, caplog) -> None:
    """库彻底不可用时连失败状态都写不进:不能再抛,只留日志。"""
    _Store(monkeypatch, _ROWS)

    def boom(*_a, **_k):
        raise RuntimeError("pg down")

    monkeypatch.setattr(runner.query_log, "summarize_window", boom)
    monkeypatch.setattr(runner.kb_ops_state, "fail_run", boom)
    with caplog.at_level(logging.WARNING, logger="official_agent.kb_ops.runner"):
        await runner.execute_run(_REQUEST, _POLICY, embed=_embed, triage=_triage)
    assert "失败状态写入失败" in caplog.text


async def test_purge_failure_does_not_fail_the_run(monkeypatch, caplog) -> None:
    store = _Store(monkeypatch, _ROWS)

    def boom(_keep):
        raise RuntimeError("pg down")

    monkeypatch.setattr(runner.kb_ops_state, "purge_old_runs", boom)
    with caplog.at_level(logging.WARNING, logger="official_agent.kb_ops.runner"):
        await runner.execute_run(_REQUEST, _POLICY, embed=_embed, triage=_triage)
    assert store.finished is not None and store.failed is None
    assert "旧运行清理失败" in caplog.text


# ── 启动:抢锁、登记、后台执行 ───────────────────────────


class _Lock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _patch_start(monkeypatch, *, lock: _Lock | None) -> dict[str, Any]:
    created: dict[str, Any] = {}

    def create_run(run_id, **kwargs):
        created.update(run_id=run_id, **kwargs)

    monkeypatch.setattr(runner.kb_ops_state, "try_acquire_run_lock", lambda: lock)
    monkeypatch.setattr(runner.kb_ops_state, "create_run", create_run)
    monkeypatch.setattr(
        runner, "get_effective_settings", lambda: _settings(embed_model="it-fake-model")
    )
    return created


async def test_start_run_refuses_while_another_run_holds_the_lock(monkeypatch) -> None:
    _patch_start(monkeypatch, lock=None)
    with pytest.raises(RunInProgressError):
        await runner.start_run(days=7, triggered_by=2)


async def test_start_run_registers_then_executes_in_background(monkeypatch) -> None:
    lock = _Lock()
    created = _patch_start(monkeypatch, lock=lock)
    executed: list[tuple[RunRequest, ClusterPolicy]] = []
    release_seen_during_run: list[bool] = []

    async def execute_run(request, policy):
        release_seen_during_run.append(lock.closed)
        executed.append((request, policy))

    monkeypatch.setattr(runner, "execute_run", execute_run)
    before = datetime.now(UTC)
    run_id = await runner.start_run(days=14, triggered_by=2)

    assert run_id.startswith("kbops_") and created["run_id"] == run_id
    assert created["triggered_by"] == 2
    assert created["window_end"] - created["window_start"] == timedelta(days=14)
    assert before <= created["window_end"] <= datetime.now(UTC)
    assert created["params"] == {
        "days": 14,
        "cluster_threshold": 0.75,
        "min_queries": 3,
        "min_threads": 2,
        "near_miss_floor": 0.37,
        "embed_model": "it-fake-model",
    }
    assert executed == []  # 触发立即返回,运行还没开始

    await asyncio.gather(*list(runner._background_tasks))
    (request, policy) = executed[0]
    assert request.run_id == run_id and request.triggered_by == 2
    assert policy == runner.cluster_policy(_settings())
    assert release_seen_during_run == [False]  # 运行期间一直持锁
    assert lock.closed is True  # 运行结束后释放
    assert not runner._background_tasks


async def test_start_run_releases_lock_when_registration_fails(monkeypatch) -> None:
    lock = _Lock()
    _patch_start(monkeypatch, lock=lock)

    def boom(_run_id, **_kwargs):
        raise RuntimeError("pg down")

    monkeypatch.setattr(runner.kb_ops_state, "create_run", boom)
    with pytest.raises(RuntimeError):
        await runner.start_run(days=7, triggered_by=2)
    assert lock.closed is True  # 否则这条连接会一直占着锁,之后谁也跑不了


async def test_start_run_releases_lock_even_if_the_run_crashes(monkeypatch) -> None:
    lock = _Lock()
    _patch_start(monkeypatch, lock=lock)

    async def execute_run(_request, _policy):
        raise RuntimeError("意外")

    monkeypatch.setattr(runner, "execute_run", execute_run)
    await runner.start_run(days=7, triggered_by=2)
    await asyncio.gather(*list(runner._background_tasks), return_exceptions=True)
    assert lock.closed is True


# ── 带起草的运行 ────────────────────────────────────────

from official_agent.kb.store import ChunkRecord, SearchHit  # noqa: E402
from official_agent.kb_ops.draft import DraftProposal  # noqa: E402
from official_agent.kb_ops.drafting import DraftRoles  # noqa: E402
from official_agent.kb_ops.verify import ConflictNote, ReviewOutput, SentenceVerdict  # noqa: E402

_FEE_CHUNK = ChunkRecord(
    chunk_id="kb_doc#2",
    source_id="kb_doc",
    title="社团章程",
    source_type="doc",
    text="本社团不向成员收取会费,日常活动经费由学院拨款和赞助支持。",
    heading_path="",
    cycle_id=12,
)
_VENUE_CHUNK = ChunkRecord(
    chunk_id="kb_venue#0",
    source_id="kb_venue",
    title="面试安排",
    source_type="faq",
    text="Q: 面试在哪里?\nA: 面试在信息楼 302。",
    heading_path="",
)
_CHUNK_BY_ID = {c.chunk_id: c for c in (_FEE_CHUNK, _VENUE_CHUNK)}


class _Reader:
    async def search(self, query: str) -> list[SearchHit]:
        return [SearchHit("kb_old", "旧说明", "入社需缴纳会费", "", 0.5, chunk_id="kb_old#0")]

    async def read_source(self, source_id: str) -> list[ChunkRecord]:
        return []

    async def get_chunks(self, chunk_ids) -> dict[str, ChunkRecord]:
        return {i: _CHUNK_BY_ID[i] for i in chunk_ids if i in _CHUNK_BY_ID}


async def _triage_both(samples, query_count, thread_count):
    """两个主题都判为该由知识库回答。"""
    fee = samples[0] != "面试在哪里"
    verdict = TriageVerdict(
        topic="社团会费" if fee else "面试地点",
        canonical_question="加入社团需要交费吗?" if fee else "面试在哪里进行?",
        scope="official_knowledge",
        reason="公开规则",
    )
    return verdict, {"input_tokens": 300, "output_tokens": 40}


def _drafting_roles(*, venue_verdict: str = "supported") -> DraftRoles:
    usage = {"input_tokens": 1000, "output_tokens": 100}

    async def draft(question: str, topic: str, reader, deadline: float):
        if topic == "社团会费":
            sentence = {
                "text": "加入社团不收取任何费用。",
                "chunk_id": "kb_doc#2",
                "quote": "本社团不向成员收取会费",
            }
            return DraftProposal(kind="new_faq", question=question, sentences=[sentence]), usage
        sentence = {
            "text": "这条 FAQ 已经说明面试地点。",
            "chunk_id": "kb_venue#0",
            "quote": "面试在信息楼 302",
        }
        proposal = DraftProposal(
            kind="alt_question",
            question="面试地点是哪",
            target_source_id="kb_venue",
            sentences=[sentence],
        )
        return proposal, usage

    async def revise(proposal, problems, chunks):
        raise AssertionError("这些用例不该走到修订")

    async def verify(proposal, chunks, neighbors):
        fee = proposal.kind == "new_faq"
        review = ReviewOutput(
            sentences=[
                SentenceVerdict(
                    index=1,
                    verdict="supported" if fee else venue_verdict,
                    quote="本社团不向成员收取会费" if fee else "面试在信息楼 302",
                )
            ],
            conflicts=[ConflictNote(source_id="kb_old", reason="旧说明说要收费")] if fee else [],
        )
        return review, usage

    return DraftRoles(draft=draft, revise=revise, verify=verify)


def _patch_dedupe_inputs(monkeypatch, *, faq=(), pending=()) -> None:
    monkeypatch.setattr(runner.kb_store, "list_faq_questions", lambda: list(faq))
    monkeypatch.setattr(runner.kb_ops_state, "list_pending_draft_questions", lambda: list(pending))


async def _embed_any(texts: list[str]) -> list[list[float]]:
    """聚类与查重共用:会费类一条轴,面试类一条轴,其余一条轴。"""

    def axis(text: str) -> int:
        if any(word in text for word in ("交钱", "收费", "交费", "会费")):
            return 0
        return 1 if "面试" in text else 2

    return [[1.0 if i == axis(t) else 0.0 for i in range(3)] for t in texts]


async def test_run_with_drafting_persists_pending_drafts(monkeypatch) -> None:
    store = _Store(monkeypatch, _ROWS, drafting=True)
    _patch_dedupe_inputs(monkeypatch, pending=[(7, "入社要钱吗,要交费吗"), (8, "社团有奖学金吗")])
    await runner.execute_run(
        _REQUEST,
        _POLICY,
        embed=_embed_any,
        triage=_triage_both,
        roles=_drafting_roles(),
        reader=_Reader(),
    )

    finished = store.finished
    assert store.failed is None and finished is not None
    assert finished["prompt_versions"] == {
        "triage": "kb_ops_triage/v1",
        "draft": "kb_ops_draft/v1",
        "revise": "kb_ops_revise/v1",
        "verify": "kb_ops_verify/v1",
    }
    stats = finished["stats"]
    assert stats["drafts"] == {"drafted": 2}
    assert stats["draft_kinds"] == {"alt_question": 1, "new_faq": 1}
    assert stats["drafts_deduped"] is True and stats["budget_exhausted"] is False
    # 两次甄别 + 两个主题各一次起草、一次审核
    assert (stats["input_tokens"], stats["output_tokens"]) == (4600, 480)
    assert [t.draft_status for t in finished["topics"]] == ["drafted", "drafted"]
    assert finished["superseded_draft_ids"] == [7]  # 同一个问题的旧草稿被顶替

    fee, venue = finished["drafts"]
    assert fee == kb_ops_state.DraftRow(
        topic_rank=1,
        kind="new_faq",
        topic="社团会费",
        question="加入社团需要交费吗?",
        answer="加入社团不收取任何费用。",
        evidence=[
            {
                "sentence": "加入社团不收取任何费用。",
                "chunk_id": "kb_doc#2",
                "quote": "本社团不向成员收取会费",
                "source_id": "kb_doc",
                "source_title": "社团章程",
            }
        ],
        target_source_id=None,
        conflict_with=("kb_old",),
        support={
            "query_count": 3,
            "thread_count": 3,
            "near_miss_count": 2,
            "sample_query_ids": [1, 2],
        },
        review={
            "findings": [{"index": 1, "verdict": "supported", "reason": ""}],
            "conflicts": [{"source_id": "kb_old", "reason": "旧说明说要收费"}],
            "revised": False,
            "downgraded": False,
            "converted_from": "",
            "mixed_cycles": False,
            "note": "",
        },
        prompt_versions=finished["prompt_versions"],
        cycle_id=12,  # 证据来自绑定了周期的条目:新条目跟着绑,不变成常青
        status="pending",
    )
    assert (venue.kind, venue.target_source_id, venue.question) == (
        "alt_question",
        "kb_venue",
        "面试地点是哪",
    )
    # 补问法沿用目标 FAQ 的答案与周期,草稿自己不带
    assert venue.answer == "" and venue.cycle_id is None and venue.status == "pending"
    assert venue.support["sample_query_ids"] == [4]


async def test_auto_rejected_draft_is_stored_as_rejected_not_pending(monkeypatch) -> None:
    store = _Store(monkeypatch, _ROWS, drafting=True)
    _patch_dedupe_inputs(monkeypatch)
    await runner.execute_run(
        _REQUEST,
        _POLICY,
        embed=_embed_any,
        triage=_triage_both,
        roles=_drafting_roles(venue_verdict="contradicted"),
        reader=_Reader(),
    )
    finished = store.finished
    assert [t.draft_status for t in finished["topics"]] == ["drafted", "auto_rejected"]
    assert finished["stats"]["drafts"] == {"auto_rejected": 1, "drafted": 1}
    assert finished["stats"]["draft_kinds"] == {"new_faq": 1}
    venue = finished["drafts"][1]
    assert venue.status == "rejected"  # 不进待审列表,留给人看它错在哪
    assert venue.review["findings"] == [
        {"index": 1, "verdict": "contradicted", "reason": "审核者判定与原文矛盾"}
    ]


async def test_new_faq_duplicating_an_existing_faq_is_converted(monkeypatch) -> None:
    store = _Store(monkeypatch, _ROWS, drafting=True)
    _patch_dedupe_inputs(
        monkeypatch, faq=[("kb_fee", "加入社团收费吗"), ("kb_venue", "面试在哪里?")]
    )
    await runner.execute_run(
        _REQUEST,
        _POLICY,
        embed=_embed_any,
        triage=_triage_both,
        roles=_drafting_roles(),
        reader=_Reader(),
    )
    fee = store.finished["drafts"][0]
    assert (fee.kind, fee.target_source_id) == ("alt_question", "kb_fee")
    assert fee.review["converted_from"] == "new_faq"
    assert fee.answer == "加入社团不收取任何费用。"  # 起草的答案留着,供人与现有答案对照
    assert fee.cycle_id is None
    assert store.finished["stats"]["draft_kinds"] == {"alt_question": 2}


async def test_only_official_knowledge_topics_are_drafted(monkeypatch) -> None:
    store = _Store(monkeypatch, _ROWS, drafting=True)
    _patch_dedupe_inputs(monkeypatch)
    drafted: list[str] = []
    roles = _drafting_roles()

    async def draft(question, topic, reader, deadline):
        drafted.append(topic)
        return await roles.draft(question, topic, reader, deadline)

    async def triage(samples, query_count, thread_count):
        verdict, usage = await _triage_both(samples, query_count, thread_count)
        if verdict.topic == "面试地点":
            verdict = verdict.model_copy(update={"scope": "personal_data"})
        return verdict, usage

    await runner.execute_run(
        _REQUEST,
        _POLICY,
        embed=_embed_any,
        triage=triage,
        roles=DraftRoles(draft=draft, revise=roles.revise, verify=roles.verify),
        reader=_Reader(),
    )
    assert drafted == ["社团会费"]
    assert [t.draft_status for t in store.finished["topics"]] == ["drafted", None]
    assert len(store.finished["drafts"]) == 1


async def test_dedupe_failure_keeps_the_reviewed_drafts(monkeypatch, caplog) -> None:
    store = _Store(monkeypatch, _ROWS, drafting=True)

    def boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(runner.kb_store, "list_faq_questions", boom)
    with caplog.at_level(logging.WARNING, logger="official_agent.kb_ops.runner"):
        await runner.execute_run(
            _REQUEST,
            _POLICY,
            embed=_embed_any,
            triage=_triage_both,
            roles=_drafting_roles(),
            reader=_Reader(),
        )
    assert store.failed is None and len(store.finished["drafts"]) == 2
    assert store.finished["stats"]["drafts_deduped"] is False  # 统计量里标出来
    assert "草稿查重失败" in caplog.text


async def test_default_reader_uses_the_currently_open_cycles(monkeypatch) -> None:
    _Store(monkeypatch, [], drafting=True)
    seen: list[tuple[int, ...]] = []

    async def open_cycle_ids() -> tuple[int, ...]:
        return (12, 13)

    class _SpyReader:
        def __init__(self, cycle_ids) -> None:
            seen.append(tuple(cycle_ids))

    monkeypatch.setattr(runner.knowledge, "open_cycle_ids", open_cycle_ids)
    monkeypatch.setattr(runner, "StoreKbReader", _SpyReader)
    await runner.execute_run(
        _REQUEST, _POLICY, embed=_embed_any, triage=_triage_both, roles=_drafting_roles()
    )
    assert seen == [(12, 13)]


def test_draft_row_flags_evidence_spanning_several_cycles() -> None:
    from official_agent.kb_ops.cluster import Phrasing, Topic
    from official_agent.kb_ops.drafting import DraftOutcome

    other_cycle = ChunkRecord("kb_b#0", "kb_b", "上届安排", "doc", "原文乙", "", cycle_id=11)
    sentences = [
        {"text": "甲。", "chunk_id": "kb_doc#2", "quote": "本社团不向成员收取会费"},
        {"text": "乙。", "chunk_id": "kb_b#0", "quote": "原文乙原文乙"},
    ]
    outcome = DraftOutcome(
        status="drafted",
        proposal=DraftProposal(kind="new_faq", question="问?", sentences=sentences),
        chunks={"kb_doc#2": _FEE_CHUNK, "kb_b#0": other_cycle},
    )
    topic = Topic(1, 3, 2, 0, None, _T0, _T0, (Phrasing("问", 3, 9),))
    verdict = TriageVerdict(
        topic="主题", canonical_question="问?", scope="official_knowledge", reason="因"
    )
    row = runner._draft_row(topic, verdict, outcome, {})
    assert row.cycle_id == 12 and row.review["mixed_cycles"] is True  # 取最新的周期,交给人定
    assert row.answer == "甲。乙。"


# ── 起草开关 ────────────────────────────────────────────


def test_drafting_is_off_by_default() -> None:
    """起草与审核的模型角色还没有在真实模型上留过 eval 基线:不显式打开就不跑。"""
    from official_agent.config import Settings

    assert Settings(_env_file=None).kb_ops_drafting_enabled is False


async def test_with_drafting_off_no_drafting_role_or_kb_read_is_touched(monkeypatch) -> None:
    store = _Store(monkeypatch, _ROWS, drafting=False)

    async def never(*_a, **_k):
        raise AssertionError("起草关着的时候不该被调用")

    class _NoReader:
        search = read_source = get_chunks = never

    monkeypatch.setattr(runner.knowledge, "open_cycle_ids", never)
    monkeypatch.setattr(runner.kb_store, "list_faq_questions", never)
    monkeypatch.setattr(runner.kb_ops_state, "list_pending_draft_questions", never)
    await runner.execute_run(
        _REQUEST,
        _POLICY,
        embed=_embed_any,
        triage=_triage_both,  # 两个主题都判为该由知识库回答,但没有人去起草
        roles=DraftRoles(draft=never, revise=never, verify=never),
        reader=_NoReader(),
    )
    finished = store.finished
    assert store.failed is None and finished is not None
    assert finished["drafts"] == [] and finished["superseded_draft_ids"] == []
    assert [t.draft_status for t in finished["topics"]] == [None, None]
    assert finished["stats"]["drafts"] == {} and finished["stats"]["triage"] == {"done": 2}
    # 运行行上只记实际用到的 prompt 版本
    assert finished["prompt_versions"] == {"triage": "kb_ops_triage/v1"}
