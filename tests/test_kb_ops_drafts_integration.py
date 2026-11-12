"""知识库运营草稿的真库集成档:草稿表、带起草的端到端运行、经管理面批准后真正入库。

运行门槛与 test_kb_integration.py 相同:KB_TEST_DATABASE_URL 指向一次性库(带 pgvector),
缺省整档跳过。嵌入用确定性的二维假实现(与 KB 集成档同一个模型名与维度,两档可以
共用一个库),三个模型角色用写死的假实现。

⚠️ 本档会清空 kb_ops_run / kb_ops_topic / kb_draft,并删改 kb_source 里本档建的条目,
只能指向一次性库;指到 agent 服务库(official_agent)时整档跳过。
"""

import contextlib
import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(
    not os.environ.get("KB_TEST_DATABASE_URL"),
    reason="需要 KB_TEST_DATABASE_URL(本地 pgvector 容器里的一次性库)",
)

from official_agent.config import Settings  # noqa: E402
from official_agent.kb import query_log  # noqa: E402
from official_agent.kb import store as kb_store  # noqa: E402
from official_agent.kb.store import SourceInput  # noqa: E402
from official_agent.kb_ops import runner  # noqa: E402
from official_agent.kb_ops.cluster import ClusterPolicy  # noqa: E402
from official_agent.kb_ops.draft import DraftProposal  # noqa: E402
from official_agent.kb_ops.drafting import DraftRoles  # noqa: E402
from official_agent.kb_ops.reader import StoreKbReader  # noqa: E402
from official_agent.kb_ops.runner import RunRequest  # noqa: E402
from official_agent.kb_ops.triage import TriageVerdict  # noqa: E402
from official_agent.kb_ops.verify import ReviewOutput, SentenceVerdict  # noqa: E402
from official_agent.state import audit as audit_state  # noqa: E402
from official_agent.state import kb_ops as kb_ops_state  # noqa: E402
from official_agent.state.kb_ops import DraftRow, TopicRow  # noqa: E402

# web_no_real_pg 会把审计写入换成空函数;批准流程的审计要落真库,先留住真函数
_REAL_WRITE_AUDIT = audit_state.write_audit

_PROD_DB_NAME = "official_agent"
_ACTOR = "it-kbops"
_POLICY = ClusterPolicy(
    similarity_threshold=0.8, min_queries=2, min_threads=2, near_miss_floor=0.37
)
_FEE_WORDS = ("交钱", "交费", "收费", "会费", "要钱")


def _url() -> str:
    return os.environ["KB_TEST_DATABASE_URL"]


async def _embed(texts: list[str]) -> list[list[float]]:
    """二维假向量:会费类问题与内容在一条轴上,其余(面试等)在另一条轴上。"""
    return [
        [1.0, 0.0] if any(word in text for word in _FEE_WORDS) else [0.0, 1.0] for text in texts
    ]


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with psycopg.connect(_url(), row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(sql: str, params: tuple = ()) -> None:
    with psycopg.connect(_url()) as conn:
        conn.execute(sql, params)
        conn.commit()


def _clean_kb() -> None:
    _execute(
        "DELETE FROM kb_source WHERE updated_by = %s OR source_id LIKE 'kb\\_draft%%'", (_ACTOR,)
    )


@pytest.fixture()
def drafts_db(monkeypatch) -> Settings:
    """数据层指到测试库;清空运营三张表与本档留下的知识条目。"""
    dbname = psycopg.conninfo.conninfo_to_dict(_url()).get("dbname") or ""
    if dbname == _PROD_DB_NAME:
        pytest.skip(f"拒绝在 agent 服务库 {_PROD_DB_NAME!r} 上跑本档:会清空运营表并改知识库")
    settings = Settings(
        _env_file=None,
        postgres_url=_url(),
        embed_model="it-fake-model",
        embed_dim=2,
        kb_ops_drafting_enabled=True,  # 起草默认关闭,本档验证的正是打开之后的行为
    )
    for module in ("state.kb_ops", "state.pools", "state.audit", "kb.query_log", "kb.store"):
        monkeypatch.setattr(f"official_agent.{module}.get_settings", lambda: settings)
    monkeypatch.setattr("official_agent.kb.schema.get_settings", lambda: settings)
    monkeypatch.setattr(runner, "get_effective_settings", lambda: settings)
    # 管理面发布走默认嵌入端点:换成假实现
    monkeypatch.setattr("official_agent.kb.store.embed_texts", _embed)
    kb_ops_state.reset_table_cache()
    query_log.reset_table_cache()
    kb_store.reset_schema_cache()
    kb_ops_state.ensure_kb_ops_tables()
    query_log.ensure_query_log_table()
    kb_store.list_faq_questions()  # 触发知识库表自举
    _execute("TRUNCATE kb_ops_run CASCADE")
    _execute("TRUNCATE kb_draft")
    _clean_kb()
    yield settings
    _clean_kb()


def _new_run(run_id: str) -> None:
    now = datetime.now(UTC)
    kb_ops_state.create_run(
        run_id, window_start=now - timedelta(days=7), window_end=now, triggered_by=2, params={}
    )


def _topic_row(rank: int = 1, **over) -> TopicRow:
    now = datetime.now(UTC)
    base = {
        "rank": rank,
        "query_count": 3,
        "thread_count": 2,
        "near_miss_count": 1,
        "best_vec_score": 0.4,
        "first_seen": now,
        "last_seen": now,
        "sample_query_ids": (1, 2),
        "triage_status": "done",
        "scope": "official_knowledge",
        "topic": "社团会费",
        "canonical_question": "加入社团需要交费吗?",
        "reason": "公开规则",
        "draft_status": "drafted",
    }
    return TopicRow(**{**base, **over})


def _draft_row(rank: int = 1, **over) -> DraftRow:
    base = {
        "topic_rank": rank,
        "kind": "new_faq",
        "topic": "社团会费",
        "question": "加入社团需要交费吗?",
        "answer": "加入社团不收取任何费用。",
        "evidence": [
            {
                "sentence": "加入社团不收取任何费用。",
                "chunk_id": "kb_doc#0",
                "quote": "本社团不向成员收取会费",
                "source_id": "kb_doc",
                "source_title": "社团章程",
            }
        ],
        "target_source_id": None,
        "conflict_with": ("kb_old",),
        "support": {"query_count": 3, "thread_count": 2, "sample_query_ids": [11, 12]},
        "review": {"revised": False, "downgraded": False, "findings": []},
        "prompt_versions": {"draft": "kb_ops_draft/v1"},
        "cycle_id": 12,
    }
    return DraftRow(**{**base, **over})


def _finish(run_id: str, drafts: list[DraftRow], **kwargs) -> None:
    _new_run(run_id)
    kb_ops_state.finish_run(
        run_id,
        stats={},
        prompt_versions={},
        topics=[_topic_row(d.topic_rank) for d in drafts],
        drafts=drafts,
        **kwargs,
    )


def _draft_ids(run_id: str) -> dict[int, int]:
    """{topic_rank: draft_id}"""
    rows = _rows("SELECT topic_rank, draft_id FROM kb_draft WHERE run_id = %s", (run_id,))
    return {r["topic_rank"]: r["draft_id"] for r in rows}


# ── 表:存量库升级、往返、状态流转 ─────────────────────────


def test_database_that_only_had_the_topic_list_is_upgraded(drafts_db) -> None:
    """存量库只有主题清单(没有草稿表、主题表没有起草结局一列):下次使用时补齐,旧主题照常可读。"""
    _new_run("kbops_old")
    kb_ops_state.finish_run(
        "kbops_old", stats={}, prompt_versions={}, topics=[_topic_row(draft_status=None)]
    )
    _execute("DROP TABLE kb_draft")
    _execute("ALTER TABLE kb_ops_topic DROP COLUMN draft_status")
    kb_ops_state.reset_table_cache()  # 相当于新版本进程启动

    (old_topic,) = kb_ops_state.list_topics("kbops_old")
    assert old_topic["topic"] == "社团会费"
    assert old_topic["draft_status"] is None and old_topic["draft_id"] is None
    assert kb_ops_state.list_drafts()["total"] == 0

    _finish("kbops_new", [_draft_row()])
    (new_topic,) = kb_ops_state.list_topics("kbops_new")
    assert new_topic["draft_status"] == "drafted"
    assert new_topic["draft_id"] == _draft_ids("kbops_new")[1]
    # 再自举一次不动数据
    kb_ops_state.reset_table_cache()
    kb_ops_state.ensure_kb_ops_tables()
    assert kb_ops_state.list_drafts()["total"] == 1


def test_draft_roundtrip_and_listing(drafts_db) -> None:
    _finish(
        "kbops_a",
        [
            _draft_row(1),
            _draft_row(2, kind="missing", answer="", evidence=[], cycle_id=None),
            _draft_row(3, kind="alt_question", target_source_id="kb_fee", status="rejected"),
        ],
    )
    ids = _draft_ids("kbops_a")
    draft = kb_ops_state.get_draft(ids[1])
    assert draft["status"] == "pending" and draft["decided_at"] is None
    assert draft["evidence"][0]["quote"] == "本社团不向成员收取会费"
    assert draft["conflict_with"] == ["kb_old"] and draft["cycle_id"] == 12
    assert draft["support"]["sample_query_ids"] == [11, 12]
    assert draft["prompt_versions"] == {"draft": "kb_ops_draft/v1"}
    assert draft["edited_by"] is None and draft["published_source_id"] is None
    # 被代码驳回的草稿不进待审,落库时就带着处理时间,没有处理人
    rejected = kb_ops_state.get_draft(ids[3])
    assert rejected["status"] == "rejected" and rejected["decided_at"] is not None
    assert rejected["decided_by"] is None

    pending = kb_ops_state.list_drafts(status="pending")
    assert pending["total"] == 2 and {d["draft_id"] for d in pending["items"]} == {ids[1], ids[2]}
    assert kb_ops_state.list_drafts(status="pending", kind="missing")["items"][0]["answer"] == ""
    everything = kb_ops_state.list_drafts(size=2)
    assert everything["total"] == 3 and len(everything["items"]) == 2
    assert kb_ops_state.list_drafts(page=2, size=2)["items"][0]["draft_id"] not in {
        d["draft_id"] for d in everything["items"]
    }
    assert kb_ops_state.list_pending_draft_questions() == [
        (ids[1], "加入社团需要交费吗?"),
        (ids[2], "加入社团需要交费吗?"),
    ]
    assert kb_ops_state.get_draft(10**9) is None


def test_draft_constraints(drafts_db) -> None:
    _new_run("kbops_c")
    with pytest.raises(psycopg.errors.CheckViolation):
        kb_ops_state.finish_run(
            "kbops_c", stats={}, prompt_versions={}, topics=[], drafts=[_draft_row(kind="essay")]
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        kb_ops_state.finish_run(
            "kbops_c", stats={}, prompt_versions={}, topics=[], drafts=[_draft_row(status="live")]
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        kb_ops_state.finish_run(
            "kbops_c",
            stats={},
            prompt_versions={},
            topics=[],
            drafts=[_draft_row(1), _draft_row(1)],
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        kb_ops_state.finish_run(
            "kbops_c",
            stats={},
            prompt_versions={},
            topics=[_topic_row(draft_status="published")],
        )
    # 上面每一次都整体回滚:没有留下半份草稿,运行也没有被记成成功
    assert kb_ops_state.list_drafts()["total"] == 0
    assert kb_ops_state.get_run("kbops_c")["status"] == "running"


def test_edit_and_decide_only_apply_to_pending_drafts(drafts_db) -> None:
    _finish("kbops_d", [_draft_row(1), _draft_row(2)])
    ids = _draft_ids("kbops_d")

    assert kb_ops_state.update_draft_content(
        ids[1], question="入社收费吗?", answer="不收费。", cycle_id=None, edited_by=5
    )
    edited = kb_ops_state.get_draft(ids[1])
    assert (edited["question"], edited["answer"], edited["cycle_id"], edited["edited_by"]) == (
        "入社收费吗?",
        "不收费。",
        None,
        5,
    )
    assert edited["updated_at"] > edited["created_at"]

    assert kb_ops_state.decide_draft(
        ids[1], status="approved", decided_by=2, published_source_id="kb_draft1"
    )
    approved = kb_ops_state.get_draft(ids[1])
    assert (approved["status"], approved["decided_by"]) == ("approved", 2)
    assert approved["published_source_id"] == "kb_draft1" and approved["decided_at"] is not None
    # 已经处理过的草稿:第二个人的决定不覆盖,也不能再改内容
    assert not kb_ops_state.decide_draft(ids[1], status="rejected", decided_by=3)
    assert not kb_ops_state.update_draft_content(
        ids[1], question="改", answer="改", cycle_id=1, edited_by=3
    )
    assert kb_ops_state.get_draft(ids[1])["status"] == "approved"
    assert kb_ops_state.get_draft(ids[1])["question"] == "入社收费吗?"


def test_new_drafts_supersede_only_pending_old_ones(drafts_db) -> None:
    _finish("kbops_e1", [_draft_row(1), _draft_row(2), _draft_row(3)])
    old = _draft_ids("kbops_e1")
    kb_ops_state.decide_draft(old[2], status="approved", decided_by=2, published_source_id="kb_x")

    _finish("kbops_e2", [_draft_row(1)], superseded_draft_ids=[old[1], old[2]])
    assert kb_ops_state.get_draft(old[1])["status"] == "superseded"
    assert kb_ops_state.get_draft(old[1])["decided_at"] is not None
    assert kb_ops_state.get_draft(old[2])["status"] == "approved"  # 人已经批准的不动
    assert kb_ops_state.get_draft(old[3])["status"] == "pending"  # 没被顶替的不动
    assert [d for d, _ in kb_ops_state.list_pending_draft_questions()] == [
        old[3],
        _draft_ids("kbops_e2")[1],
    ]


def test_drafts_outlive_their_run_and_old_decided_drafts_are_purged(drafts_db) -> None:
    _finish("kbops_f1", [_draft_row(1), _draft_row(2), _draft_row(3)])
    ids = _draft_ids("kbops_f1")
    kb_ops_state.decide_draft(ids[2], status="rejected", decided_by=2)
    kb_ops_state.decide_draft(ids[3], status="approved", decided_by=2, published_source_id="kb_x")
    _execute(
        "UPDATE kb_draft SET decided_at = now() - interval '400 days' WHERE draft_id = %s",
        (ids[3],),
    )
    _execute(
        "UPDATE kb_draft SET created_at = now() - interval '900 days' WHERE draft_id = %s",
        (ids[1],),
    )

    # 运行只留最近一次:主题随运行删除,草稿留着等人处理
    _finish("kbops_f2", [])
    assert kb_ops_state.purge_old_runs(1) == 1
    assert kb_ops_state.list_topics("kbops_f1") == []
    assert kb_ops_state.list_drafts()["total"] == 3

    assert kb_ops_state.purge_decided_drafts(365) == 1  # 只删处理完且超过保留期的
    remaining = {d["draft_id"]: d["status"] for d in kb_ops_state.list_drafts()["items"]}
    assert remaining == {ids[1]: "pending", ids[2]: "rejected"}  # 待审的再旧也不删


def test_draft_lock_is_exclusive_per_draft(drafts_db) -> None:
    first = kb_ops_state.try_acquire_draft_lock(41)
    assert first is not None
    try:
        assert kb_ops_state.try_acquire_draft_lock(41) is None  # 另一个请求在处理同一份
        other = kb_ops_state.try_acquire_draft_lock(42)  # 别的草稿不受影响
        assert other is not None
        other.close()
    finally:
        first.close()
    again = kb_ops_state.try_acquire_draft_lock(41)
    assert again is not None
    again.close()


# ── 端到端:合成日志 + 真知识库 → 草稿 → 批准入库 ────────────

_FEE_TEXT = "本社团不向成员收取会费,日常活动经费由学院拨款和赞助支持。"


class _Log:
    def __init__(self) -> None:
        self.tag = secrets.token_hex(4)
        self.start = datetime(2001, 1, 1, tzinfo=UTC) + timedelta(days=secrets.randbelow(6900))
        self.end = self.start + timedelta(days=7)
        self._minute = 0

    def add(self, thread: str, query: str, best: float | None = 0.1) -> int:
        self._minute += 1
        with psycopg.connect(_url()) as conn:
            row = conn.execute(
                """
                INSERT INTO kb_query_log
                    (thread_id, trace_id, query, status, best_vec_score, created_at)
                VALUES (%s, %s, %s, 'not_found', %s, %s) RETURNING id
                """,
                (
                    f"it:kbdraft:{self.tag}:{thread}",
                    "0" * 32,
                    query,
                    best,
                    self.start + timedelta(minutes=self._minute),
                ),
            ).fetchone()
            conn.commit()
        return int(row[0])

    def cleanup(self) -> None:
        _execute("DELETE FROM kb_query_log WHERE thread_id LIKE %s", (f"it:kbdraft:{self.tag}:%",))


@pytest.fixture()
def log(drafts_db) -> Any:
    log = _Log()
    yield log
    log.cleanup()


async def _seed_kb() -> dict[str, str]:
    """知识库里有:一条提到会费的正文(绑定在第 12 届)、一条面试地点的 FAQ。"""
    charter = await kb_store.ingest_source(
        SourceInput(
            title="社团章程",
            type="doc",
            content_md=f"# 经费\n{_FEE_TEXT}",
            updated_by=_ACTOR,
            cycle_id=12,
        ),
        embedder=_embed,
    )
    venue = await kb_store.ingest_source(
        SourceInput(
            title="面试地点",
            type="faq",
            question="面试在哪里?",
            answer="面试在信息楼 302。",
            updated_by=_ACTOR,
        ),
        embedder=_embed,
    )
    return {"charter": charter, "venue": venue}


def _seed_log(log: _Log) -> None:
    log.add("u1", "入社要交钱吗", best=0.42)
    log.add("u2", "加入社团有会费吗", best=0.40)
    log.add("u3", "入社要交钱吗", best=0.41)
    log.add("u4", "面试地点是哪", best=0.30)
    log.add("u5", "面试地点是哪", best=0.31)


async def _triage(samples, query_count, thread_count):
    fee = any(word in samples[0] for word in _FEE_WORDS)
    verdict = TriageVerdict(
        topic="社团会费" if fee else "面试地点",
        canonical_question="加入社团需要交费吗?" if fee else "面试地点是哪?",
        scope="official_knowledge",
        reason="公开信息",
    )
    return verdict, {"input_tokens": 100, "output_tokens": 10}


def _roles() -> DraftRoles:
    """照着真实检索结果起草的假角色:引用的块 id 都是从知识库里查出来的。"""
    usage = {"input_tokens": 500, "output_tokens": 50}

    async def draft(question: str, topic: str, reader, deadline: float):
        hits = await reader.search(question)
        if topic == "社团会费":
            hit = next(h for h in hits if "会费" in h.chunk_text)
            sentences = [
                {
                    "text": "加入社团不收取任何费用。",
                    "chunk_id": hit.chunk_id,
                    "quote": "本社团不向成员收取会费",
                }
            ]
            return DraftProposal(kind="new_faq", question=question, sentences=sentences), usage
        hit = next(h for h in hits if h.source_type == "faq")
        proposal = DraftProposal(
            kind="alt_question",
            question=question,
            target_source_id=hit.source_id,
            sentences=[
                {
                    "text": "这条 FAQ 已经说明面试地点。",
                    "chunk_id": hit.chunk_id,
                    "quote": "面试在信息楼 302",
                }
            ],
        )
        return proposal, usage

    async def revise(proposal, problems, chunks):
        raise AssertionError("夹具里的草稿都有出处,不该走到修订")

    async def verify(proposal, chunks, neighbors):
        review = ReviewOutput(
            sentences=[
                SentenceVerdict(index=i, verdict="supported", quote=s.quote)
                for i, s in enumerate(proposal.sentences, start=1)
            ]
        )
        return review, usage

    return DraftRoles(draft=draft, revise=revise, verify=verify)


async def _run_with_drafting(log: _Log, *, cycle_ids: tuple[int, ...] = (12,)) -> str:
    request = RunRequest(
        run_id=f"kbops_{log.tag}", window_start=log.start, window_end=log.end, triggered_by=2
    )
    kb_ops_state.create_run(
        request.run_id,
        window_start=request.window_start,
        window_end=request.window_end,
        triggered_by=2,
        params={},
    )
    await runner.execute_run(
        request,
        _POLICY,
        embed=_embed,
        triage=_triage,
        roles=_roles(),
        reader=StoreKbReader(cycle_ids, embedder=_embed),
    )
    return request.run_id


async def test_run_with_drafting_end_to_end(log) -> None:
    kb = await _seed_kb()
    _seed_log(log)
    run_id = await _run_with_drafting(log)

    run = kb_ops_state.get_run(run_id)
    assert run["status"] == "succeeded", run["error"]
    assert run["stats"]["drafts"] == {"drafted": 2}
    assert run["stats"]["draft_kinds"] == {"alt_question": 1, "new_faq": 1}
    assert run["stats"]["drafts_deduped"] is True
    assert run["prompt_versions"] == {
        "triage": "kb_ops_triage/v1",
        "draft": "kb_ops_draft/v1",
        "revise": "kb_ops_revise/v1",
        "verify": "kb_ops_verify/v1",
    }

    fee_topic, venue_topic = kb_ops_state.list_topics(run_id)
    assert (fee_topic["topic"], fee_topic["draft_status"]) == ("社团会费", "drafted")
    assert venue_topic["draft_status"] == "drafted"
    fee = kb_ops_state.get_draft(fee_topic["draft_id"])
    venue = kb_ops_state.get_draft(venue_topic["draft_id"])

    assert (fee["kind"], fee["status"], fee["question"]) == (
        "new_faq",
        "pending",
        "加入社团需要交费吗?",
    )
    assert fee["answer"] == "加入社团不收取任何费用。"
    (evidence,) = fee["evidence"]
    assert evidence["source_id"] == kb["charter"] and evidence["source_title"] == "社团章程"
    assert evidence["chunk_id"] == f"{kb['charter']}#0"
    assert fee["cycle_id"] == 12  # 证据来自绑定了周期的条目,草稿跟着绑
    assert fee["support"]["query_count"] == 3 and fee["support"]["thread_count"] == 3
    assert len(fee["support"]["sample_query_ids"]) == 2
    assert fee["review"]["findings"] == [{"index": 1, "verdict": "supported", "reason": ""}]

    assert (venue["kind"], venue["target_source_id"]) == ("alt_question", kb["venue"])
    assert venue["question"] == "面试地点是哪?" and venue["answer"] == ""

    # 这次运行什么都没有发布:知识库还是那两条
    assert {r["source_id"] for r in _rows("SELECT source_id FROM kb_source")} >= set(kb.values())
    assert not _rows("SELECT 1 FROM kb_source WHERE source_id LIKE 'kb\\_draft%%'")
    assert kb_store.get_source(kb["venue"]).alt_questions == []


async def test_stale_cycle_content_is_not_drafted_from(log) -> None:
    """换届之后:绑定在上一届的条目不在开放周期里,起草检索不到它,也引用不了它。"""
    await _seed_kb()
    _seed_log(log)
    run_id = await _run_with_drafting(log, cycle_ids=(13,))

    fee_topic, venue_topic = kb_ops_state.list_topics(run_id)
    # 假角色在检索结果里找不到提到会费的块 → 起草失败,没有草稿;面试那条是常青的,照常
    assert (fee_topic["draft_status"], fee_topic["draft_id"]) == ("failed", None)
    assert venue_topic["draft_status"] == "drafted"
    assert kb_ops_state.list_drafts(status="pending")["total"] == 1


async def test_rerun_supersedes_the_previous_pending_drafts(log) -> None:
    await _seed_kb()
    _seed_log(log)
    first = await _run_with_drafting(log)
    first_ids = _draft_ids(first)

    log.tag = secrets.token_hex(4)  # 第二次运行换一个 run_id,读的是同一段日志
    second = await _run_with_drafting(log)
    second_ids = _draft_ids(second)

    assert {kb_ops_state.get_draft(i)["status"] for i in first_ids.values()} == {"superseded"}
    assert {kb_ops_state.get_draft(i)["status"] for i in second_ids.values()} == {"pending"}
    assert kb_ops_state.list_drafts(status="pending")["total"] == 2  # 待审列表里不会堆出两份


def test_admin_api_review_and_publish_real_pg(log, monkeypatch, web_no_real_pg) -> None:
    """真实 HTTP → 鉴权 → 草稿锁 → 审计 → 知识库入库 → 草稿状态,全链打在真库上。"""
    import asyncio

    from fastapi.testclient import TestClient

    from official_agent.web.app import create_app

    kb = asyncio.run(_seed_kb())
    _seed_log(log)
    run_id = asyncio.run(_run_with_drafting(log))
    # asyncio.run 各自一个事件循环:知识库连接池是同步的,不受影响;嵌入是假实现
    fee_topic, venue_topic = kb_ops_state.list_topics(run_id)
    fee_id, venue_id = fee_topic["draft_id"], venue_topic["draft_id"]
    # 再放一份「待补充」草稿:官方材料里没有答案,等人写
    _finish(
        "kbops_manual",
        [
            _draft_row(
                1,
                kind="missing",
                answer="",
                evidence=[],
                cycle_id=None,
                topic="奖学金",
                question="社团有奖学金吗?",
                conflict_with=(),
            )
        ],
    )
    missing_id = _draft_ids("kbops_manual")[1]

    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield object()

    async def _resolve(*_a: object, **_k: object) -> dict:
        return {"user_id": 2, "role": "admin", "permission_codes": ["kb:manage"], "source": "web"}

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    monkeypatch.setattr(audit_state, "write_audit", _REAL_WRITE_AUDIT)
    audit_state.ensure_audit_table()

    auth = {"Authorization": "Bearer tok"}
    base = "/api/agent/admin/kb/ops/drafts"

    def audits(draft_id: int) -> list[dict[str, Any]]:
        return _rows(
            "SELECT acting_user_id, channel, agent, action, decision FROM agent_audit_log "
            "WHERE thread_id = %s ORDER BY id",
            (f"kbdraft:{draft_id}",),
        )

    _execute("DELETE FROM agent_audit_log WHERE thread_id LIKE 'kbdraft:%%'")
    with TestClient(create_app()) as client:
        listed = client.get(base, headers=auth).json()
        assert listed["total"] == 3
        assert {d["draft_id"] for d in listed["items"]} == {fee_id, venue_id, missing_id}
        fee_view = next(d for d in listed["items"] if d["draft_id"] == fee_id)
        assert [s["query"] for s in fee_view["samples"]] == ["入社要交钱吗", "加入社团有会费吗"]
        assert "sample_query_ids" not in fee_view["support"]

        # ── 批准新 FAQ:知识库里多出一条,带着周期绑定,能被检索到 ──
        approved = client.post(f"{base}/{fee_id}/approve", headers=auth)
        assert approved.status_code == 200, approved.text
        source_id = approved.json()["published_source_id"]
        assert source_id == f"kb_draft{fee_id}" and approved.json()["audit_recorded"] is True
        published = kb_store.get_source(source_id)
        assert (published.type, published.title, published.question, published.answer) == (
            "faq",
            "社团会费",
            "加入社团需要交费吗?",
            "加入社团不收取任何费用。",
        )
        assert published.cycle_id == 12 and published.updated_by == "user:2" and published.enabled
        hits = asyncio.run(
            kb_store.search("入社要交钱吗", embedder=_embed, min_score=0.45, cycle_ids=(12,))
        )
        assert source_id in {h.source_id for h in hits}
        draft = kb_ops_state.get_draft(fee_id)
        assert (draft["status"], draft["decided_by"], draft["published_source_id"]) == (
            "approved",
            2,
            source_id,
        )
        intent, result = audits(fee_id)
        assert intent["action"]["op"] == "approve_kb_draft_intent"
        assert intent["action"]["prompt_versions"]["verify"] == "kb_ops_verify/v1"
        assert result["action"]["op"] == "approve_kb_draft"
        assert result["action"]["source_id"] == source_id
        assert (result["acting_user_id"], result["channel"], result["decision"]) == (
            2,
            "kb_ops",
            "u2:approve",
        )
        # 再点一次批准:已处理,不会再发布一遍
        again = client.post(f"{base}/{fee_id}/approve", headers=auth)
        assert again.status_code == 409 and len(audits(fee_id)) == 2
        assert len(_rows("SELECT 1 FROM kb_source WHERE source_id = %s", (source_id,))) == 1

        # ── 批准补问法:目标 FAQ 多一种问法、多一块,答案不变 ──
        detail = client.get(f"{base}/{venue_id}", headers=auth).json()
        assert detail["target"]["source_id"] == kb["venue"]
        assert detail["target"]["answer"] == "面试在信息楼 302。"
        approved = client.post(f"{base}/{venue_id}/approve", headers=auth)
        assert approved.status_code == 200, approved.text
        assert approved.json()["published_source_id"] == kb["venue"]
        target = kb_store.get_source(kb["venue"])
        assert target.alt_questions == ["面试地点是哪?"] and target.chunk_count == 2
        assert target.answer == "面试在信息楼 302。" and target.updated_by == "user:2"

        # ── 「待补充」:没有答案不能批准;人补上答案后发布成常青 FAQ ──
        refused = client.post(f"{base}/{missing_id}/approve", headers=auth)
        assert refused.status_code == 400 and "还没有答案" in refused.json()["detail"]
        assert audits(missing_id) == []  # 内容不齐:连意图审计都不该有
        edited = client.put(
            f"{base}/{missing_id}",
            headers=auth,
            json={"question": "社团有奖学金吗?", "answer": "社团不设奖学金。"},
        )
        assert edited.status_code == 200
        approved = client.post(f"{base}/{missing_id}/approve", headers=auth)
        assert approved.status_code == 200, approved.text
        manual = kb_store.get_source(f"kb_draft{missing_id}")
        assert manual.answer == "社团不设奖学金。" and manual.cycle_id is None
        assert audits(missing_id)[0]["action"]["edited_by"] == 2  # 审计里看得出人改过

        assert client.get(base, headers=auth).json()["total"] == 0  # 待审列表清空
        assert client.get(f"{base}?status=approved", headers=auth).json()["total"] == 3


def test_admin_api_reject_real_pg(drafts_db, monkeypatch, web_no_real_pg) -> None:
    from fastapi.testclient import TestClient

    from official_agent.web.app import create_app

    _finish("kbops_r", [_draft_row(1), _draft_row(2)])
    ids = _draft_ids("kbops_r")

    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield object()

    async def _resolve(*_a: object, **_k: object) -> dict:
        return {"user_id": 2, "role": "admin", "permission_codes": ["kb:manage"], "source": "web"}

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    monkeypatch.setattr(audit_state, "write_audit", _REAL_WRITE_AUDIT)
    audit_state.ensure_audit_table()
    _execute("DELETE FROM agent_audit_log WHERE thread_id LIKE 'kbdraft:%%'")
    auth = {"Authorization": "Bearer tok"}
    base = "/api/agent/admin/kb/ops/drafts"

    with TestClient(create_app()) as client:
        rejected = client.post(f"{base}/{ids[1]}/reject", headers=auth)
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
        draft = kb_ops_state.get_draft(ids[1])
        assert (draft["status"], draft["decided_by"]) == ("rejected", 2)
        assert draft["published_source_id"] is None
        (audit_row,) = _rows(
            "SELECT action, decision FROM agent_audit_log WHERE thread_id = %s",
            (f"kbdraft:{ids[1]}",),
        )
        assert (
            audit_row["action"]["op"] == "reject_kb_draft" and audit_row["decision"] == "u2:reject"
        )
        assert not _rows("SELECT 1 FROM kb_source WHERE source_id LIKE 'kb\\_draft%%'")
        # 驳回过的不能再批准或再驳回
        assert client.post(f"{base}/{ids[1]}/approve", headers=auth).status_code == 409
        assert client.post(f"{base}/{ids[1]}/reject", headers=auth).status_code == 409

        # 另一个请求正持有这份草稿的锁(比如正在批准):驳回与修改都进不来
        lock = kb_ops_state.try_acquire_draft_lock(ids[2])
        try:
            assert client.post(f"{base}/{ids[2]}/reject", headers=auth).status_code == 409
            body = {"question": "问?", "answer": "答。"}
            assert client.put(f"{base}/{ids[2]}", headers=auth, json=body).status_code == 409
        finally:
            lock.close()
        assert kb_ops_state.get_draft(ids[2])["status"] == "pending"
