"""KB 数据面单测——mock 连接验证 SQL/参数/映射(threads.py 先例)。

真库往返(向量相似度/HNSW/级联)由 test_kb_integration.py 集成档覆盖。
"""

from unittest.mock import MagicMock, patch

import pytest

from official_agent.kb import store
from official_agent.kb.store import KbValidationError, SourceInput


def _mock_conn(fetchone=None, fetchall=None, rowcount=1):
    """支持 with _conn() as conn 协议的 mock 连接;execute 恒返回同一游标语义。"""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (lambda: fetchone) if fetchone is not None else (lambda: None)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return conn


@pytest.fixture(autouse=True)
def _fresh_schema_cache():
    """建表结果按进程缓存;每个用例 patch 的 ensure 返回值不同,先清缓存。"""
    store.reset_schema_cache()
    yield
    store.reset_schema_cache()


def _source(**over) -> SourceInput:
    base = {
        "title": "技术部介绍",
        "type": "doc",
        "content_md": "# 技术部\n写代码。",
        "updated_by": "admin-1",
    }
    base.update(over)
    return SourceInput(**base)


def _patch_write(monkeypatch, conn, version=3):
    monkeypatch.setattr(store, "_conn", lambda: conn)
    monkeypatch.setattr(
        "official_agent.kb.store.current_embed_target", lambda: ("fake-model", 4)
    )
    monkeypatch.setattr(
        "official_agent.kb.store.ensure_kb_schema", lambda conn, *, embed_model, dim: version
    )


def _fake_embedder(dim=4):
    async def fake(texts):
        return [[0.1] * dim for _ in texts]

    return fake


# ── 校验 ────────────────────────────────────────────────


def test_faq_missing_answer_rejected() -> None:
    with pytest.raises(KbValidationError, match="question 与 answer"):
        store._split_chunks(_source(type="faq", question="问", answer=" "))


def test_doc_empty_content_rejected() -> None:
    with pytest.raises(KbValidationError, match="content_md"):
        store._split_chunks(_source(content_md="  "))


def test_unknown_type_and_kind_rejected() -> None:
    with pytest.raises(KbValidationError, match="source_type"):
        store._split_chunks(_source(type="pdf"))
    with pytest.raises(KbValidationError, match="kind"):
        store._split_chunks(_source(kind="prod"))


# ── 入库 ────────────────────────────────────────────────


async def test_ingest_doc_writes_source_content_chunks(monkeypatch) -> None:
    conn = _mock_conn(fetchone={"embed_model": "fake-model", "dim": 4, "version": 3})
    _patch_write(monkeypatch, conn)
    sid = await store.ingest_source(_source(), embedder=_fake_embedder())
    assert sid.startswith("kb_")
    sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("INSERT INTO kb_source" in s and "ON CONFLICT" in s for s in sqls)
    assert any("INSERT INTO kb_doc" in s for s in sqls)
    assert any("DELETE FROM kb_chunks" in s for s in sqls)
    chunk_inserts = [
        c for c in conn.execute.call_args_list if "INSERT INTO kb_chunks" in c.args[0]
    ]
    assert len(chunk_inserts) == 1
    params = chunk_inserts[0].args[1]
    assert params[1] == sid  # source_id
    assert params[5].startswith("[")  # pgvector 文本入参
    assert params[6] == 3  # model_version = ensure 返回的 version


async def test_ingest_faq_writes_faq_table(monkeypatch) -> None:
    conn = _mock_conn()
    _patch_write(monkeypatch, conn)
    await store.ingest_source(
        _source(type="faq", question="怎么报名?", answer="官网填表"),
        embedder=_fake_embedder(),
    )
    sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("INSERT INTO kb_faq" in s for s in sqls)
    faq_params = next(
        c.args[1] for c in conn.execute.call_args_list if "INSERT INTO kb_faq" in c.args[0]
    )
    assert faq_params[1] == "怎么报名?" and faq_params[2] == "官网填表"


async def test_ingest_type_migration_clears_both_content_tables(monkeypatch) -> None:
    conn = _mock_conn()
    _patch_write(monkeypatch, conn)
    await store.ingest_source(
        _source(type="faq", question="问", answer="答"), embedder=_fake_embedder()
    )
    sqls = [c.args[0] for c in conn.execute.call_args_list]
    # 两表都先 DELETE,faq/doc 迁移不残留旧内容
    assert any("DELETE FROM kb_faq" in s for s in sqls)
    assert any("DELETE FROM kb_doc" in s for s in sqls)


async def test_ingest_respects_explicit_source_id_and_updated_by(monkeypatch) -> None:
    conn = _mock_conn()
    _patch_write(monkeypatch, conn)
    sid = await store.ingest_source(
        _source(updated_by="ops-9"), embedder=_fake_embedder(), source_id="kb_fixed"
    )
    assert sid == "kb_fixed"
    src_params = next(
        c.args[1] for c in conn.execute.call_args_list if "INSERT INTO kb_source" in c.args[0]
    )
    assert src_params[0] == "kb_fixed"
    assert src_params[5] == "ops-9"  # updated_by
    assert src_params[6] is None  # 不指定周期 = 常青条目


async def test_ingest_writes_cycle_id_and_overwrites_on_update(monkeypatch) -> None:
    """周期绑定随条目落库;更新时照写(改回常青要能把旧周期清掉)。"""
    conn = _mock_conn()
    _patch_write(monkeypatch, conn)
    await store.ingest_source(_source(cycle_id=12), embedder=_fake_embedder())
    sql, params = next(
        c.args for c in conn.execute.call_args_list if "INSERT INTO kb_source" in c.args[0]
    )
    assert params[6] == 12
    assert "cycle_id = EXCLUDED.cycle_id" in sql


async def test_ingest_vector_count_mismatch_rejected() -> None:
    """embedder 返回条数与块数不一致 → 写库前即抛,不碰连接。"""

    async def empty_embedder(texts):
        return []

    with pytest.raises(RuntimeError, match="不一致"):
        await store.ingest_source(_source(), embedder=empty_embedder)


# ── 启停/删除/查询 ────────────────────────────────────────


def test_set_enabled_scoped_and_signed(monkeypatch) -> None:
    conn = _mock_conn(rowcount=1)
    monkeypatch.setattr(store, "_conn", lambda: conn)
    assert store.set_source_enabled("kb_1", False, updated_by="ops-2") is True
    sql, params = conn.execute.call_args.args
    assert "enabled = %s" in sql and "updated_by = %s" in sql
    assert params == (False, "ops-2", "kb_1")


def test_delete_returns_false_when_missing() -> None:
    conn = _mock_conn(rowcount=0)
    with patch.object(store, "_conn", lambda: conn):
        assert store.delete_source("nope") is False


def test_get_source_missing_returns_none() -> None:
    conn = _mock_conn(fetchone=None)
    with patch.object(store, "_conn", lambda: conn):
        assert store.get_source("nope") is None


def test_get_source_maps_faq_content() -> None:
    row = {
        "source_id": "kb_1",
        "source_title": "FAQ",
        "source_type": "faq",
        "kind": "normal",
        "tags": ["招新"],
        "enabled": True,
        "updated_by": "ops",
    }
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.side_effect = [
        MagicMock(fetchone=lambda: row),  # source 行
        MagicMock(fetchone=lambda: {"question": "Q", "answer": "A"}),  # faq 内容
        MagicMock(fetchone=lambda: {"n": 1}),  # 块数
    ]
    with patch.object(store, "_conn", lambda: conn):
        rec = store.get_source("kb_1")
    assert rec is not None
    assert rec.question == "Q" and rec.answer == "A" and rec.content_md == ""
    assert rec.chunk_count == 1 and rec.tags == ["招新"]
    assert rec.cycle_id is None


def test_get_source_maps_cycle_id() -> None:
    row = {
        "source_id": "kb_2",
        "source_title": "本届面试安排",
        "source_type": "doc",
        "kind": "normal",
        "tags": [],
        "enabled": True,
        "updated_by": "ops",
        "cycle_id": 12,
    }
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.side_effect = [
        MagicMock(fetchone=lambda: row),
        MagicMock(fetchone=lambda: None),  # 不是 faq
        MagicMock(fetchone=lambda: {"content_md": "# 安排"}),
        MagicMock(fetchone=lambda: {"n": 1}),
    ]
    with patch.object(store, "_conn", lambda: conn):
        rec = store.get_source("kb_2")
    assert rec is not None and rec.cycle_id == 12
    assert "cycle_id" in conn.execute.call_args_list[0].args[0]


def test_list_sources_applies_kind_and_keyword_filters() -> None:
    conn = _mock_conn(fetchone={"n": 0}, fetchall=[])
    with patch.object(store, "_conn", lambda: conn):
        result = store.list_sources(kind="test", keyword="评测", page=2, size=10)
    assert result["total"] == 0 and result["page"] == 2
    list_sql = conn.execute.call_args_list[-1].args[0]
    assert "kind = %s" in list_sql and "ILIKE" in list_sql
    params = conn.execute.call_args_list[-1].args[1]
    assert params[0] == "test" and params[1] == "%评测%" and params[2] == 10 and params[3] == 10


# ── 检索 ────────────────────────────────────────────────


def _patch_search(monkeypatch, conn, version=7) -> None:
    monkeypatch.setattr(store, "_conn", lambda: conn)
    monkeypatch.setattr(
        "official_agent.kb.store.current_embed_target", lambda: ("fake-model", 4)
    )
    monkeypatch.setattr(
        "official_agent.kb.store.ensure_kb_schema",
        lambda conn, *, embed_model, dim: version,
    )


def _row(source_id: str, score: float, matched: int = 0, ordinal: int = 0) -> dict:
    return {
        "source_id": source_id,
        "source_title": f"标题{source_id}",
        "chunk_text": "片段",
        "heading_path": "",
        "chunk_ordinal": ordinal,
        "score": score,
        "matched": matched,
    }


def test_search_excludes_test_and_disabled_by_default(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[_row("kb_a", 0.87)])
    _patch_search(monkeypatch, conn)
    outcome = store._search_sync([0.1, 0.2], top_k=4, include_test=False, query="技术部做什么")
    hits = outcome.hits
    assert len(hits) == 1
    assert hits[0].source_id == "kb_a" and hits[0].score == pytest.approx(0.87)
    sql = conn.execute.call_args.args[0]
    assert "kind = 'normal'" in sql  # test 条目隔离
    assert "s.enabled" in sql  # 停用退出生检索
    assert "model_version = %(version)s" in sql  # 禁跨模型混排
    params = conn.execute.call_args.args[1]
    assert params["version"] == 7  # version 过滤
    assert params["patterns"] == ["%技术部%"]  # 疑问词不进字面匹配


def test_search_without_cycle_only_sees_evergreen_sources(monkeypatch) -> None:
    """不给周期:过滤条件仍在,周期数组为空 → 只剩 cycle_id IS NULL 的常青条目。"""
    conn = _mock_conn(fetchall=[])
    _patch_search(monkeypatch, conn)
    store._search_sync([0.1], top_k=2, include_test=False)
    sql, params = conn.execute.call_args.args
    # 向量、关键词两路都要过滤,否则往届条目能从另一路混进候选
    assert sql.count("s.cycle_id IS NULL OR s.cycle_id = ANY(%(cycle_ids)s::int[])") == 2
    assert params["cycle_ids"] == []


def test_search_passes_open_cycles_to_filter(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[])
    _patch_search(monkeypatch, conn)
    store._search_sync([0.1], top_k=2, include_test=False, cycle_ids=(12, 13))
    assert conn.execute.call_args.args[1]["cycle_ids"] == [12, 13]


async def test_search_forwards_cycle_ids_and_returns_hits_only(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[_row("kb_a", 0.87)])
    _patch_search(monkeypatch, conn)
    monkeypatch.setattr(
        "official_agent.kb.store.get_settings",
        lambda: MagicMock(kb_min_score=0.45, kb_keyword_min_ratio=0.5, postgres_url="x"),
    )
    hits = await store.search("技术部做什么", embedder=_fake_embedder(), cycle_ids=[12])
    assert [h.source_id for h in hits] == ["kb_a"]
    assert conn.execute.call_args.args[1]["cycle_ids"] == [12]


def test_search_reports_best_scores_even_when_nothing_passes(monkeypatch) -> None:
    """没命中也要带回过门槛之前的最高分:检索日志靠它分辨「差一点」与「完全没有」。"""
    conn = _mock_conn(fetchall=[_row("kb_a", 0.42, matched=1), _row("kb_b", 0.31, matched=0)])
    _patch_search(monkeypatch, conn)
    outcome = store._search_sync(
        [0.1],
        top_k=4,
        include_test=False,
        query="计拔怎么报名",
        min_score=0.45,
        keyword_min_ratio=1.0,
    )
    assert outcome.hits == []
    assert outcome.best_vec_score == pytest.approx(0.42)
    assert outcome.best_kw_ratio is not None and 0 < outcome.best_kw_ratio < 1


def test_best_signals_edge_cases() -> None:
    # 没有候选:两项都没有值(区别于「有候选但分数是 0」)
    assert store._best_signals([], n_terms=2) == (None, None)
    # 问题里抽不出关键词:关键词命中比无从谈起
    assert store._best_signals([_row("kb_a", 0.5)], n_terms=0) == (0.5, None)
    # 两路各取最高,不要求来自同一个块
    rows = [_row("kb_a", 0.9, matched=0), _row("kb_b", 0.2, matched=3)]
    assert store._best_signals(rows, n_terms=4) == (0.9, 0.75)


def test_search_include_test_drops_kind_filter(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[])
    _patch_search(monkeypatch, conn)
    store._search_sync([0.1], top_k=2, include_test=True)
    sql = conn.execute.call_args.args[0]
    assert "kind = 'normal'" not in sql


def test_schema_bootstrap_runs_once_per_process(monkeypatch) -> None:
    """建表只在第一次检索时跑;之后的检索不再执行任何 DDL(no-op DDL 也持表锁)。"""
    calls: list[int] = []
    conn = _mock_conn(fetchall=[])
    monkeypatch.setattr(store, "_conn", lambda: conn)
    monkeypatch.setattr(
        "official_agent.kb.store.current_embed_target", lambda: ("fake-model", 4)
    )

    def _ensure(conn, *, embed_model, dim):
        calls.append(1)
        return 3

    monkeypatch.setattr("official_agent.kb.store.ensure_kb_schema", _ensure)
    for _ in range(3):
        store._search_sync([0.1], top_k=2, include_test=False)
    assert len(calls) == 1
    # 换模型 → 重新建表(版本会 bump,缓存不能沿用)
    monkeypatch.setattr(
        "official_agent.kb.store.current_embed_target", lambda: ("other-model", 4)
    )
    store._search_sync([0.1], top_k=2, include_test=False)
    assert len(calls) == 2


def test_fuse_drops_candidates_below_threshold() -> None:
    """门槛生效:不相关的问题要能返回空表,「查不到」才可能被模型说出来。"""
    rows = [_row("kb_a", 0.31), _row("kb_b", 0.28)]
    assert store._fuse(rows, n_terms=2, top_k=4, min_score=0.45, keyword_min_ratio=0.5) == []


def test_fuse_keyword_hit_rescues_low_vector_score() -> None:
    """专有名词兜底:向量分不够,但问题关键词过半字面出现在块里,照样召回。"""
    rows = [_row("kb_a", 0.40, matched=2), _row("kb_b", 0.42, matched=0)]
    hits = store._fuse(rows, n_terms=3, top_k=4, min_score=0.45, keyword_min_ratio=0.5)
    assert [h.source_id for h in hits] == ["kb_a"]
    assert hits[0].keyword_score == pytest.approx(0.667)


def test_fuse_rrf_prefers_hits_strong_on_both_arms() -> None:
    """两路都靠前的块胜过只在一路第一的块。"""
    rows = [
        _row("vec_only", 0.80, matched=0),
        _row("both", 0.78, matched=2),
        _row("other", 0.70, matched=0),
        _row("kw_only", 0.50, matched=3),
    ]
    hits = store._fuse(rows, n_terms=3, top_k=4, min_score=0.45, keyword_min_ratio=0.5)
    assert hits[0].source_id == "both"


def test_fuse_tiebreak_is_deterministic() -> None:
    rows = [_row("kb_b", 0.6, ordinal=1), _row("kb_a", 0.6, ordinal=0)]
    hits = store._fuse(rows, n_terms=0, top_k=2, min_score=0.0, keyword_min_ratio=0.5)
    assert [h.source_id for h in hits] == ["kb_a", "kb_b"]


def test_escape_like_neutralises_wildcards() -> None:
    assert store._escape_like("100%_a\\b") == "100\\%\\_a\\\\b"


def test_to_pgvector_format() -> None:
    assert store._to_pgvector([0.5, -1]) == "[0.5,-1.0]"


# ── FAQ 附加问法 ─────────────────────────────────────────


def test_same_question_ignores_whitespace_and_case() -> None:
    assert store.same_question("入社 要钱吗", "入社要钱吗\n")
    assert store.same_question("FAQ 在哪", "faq在哪")
    assert not store.same_question("入社要钱吗", "入社要钱吗?")  # 标点不同算不同:宁可多留一种问法


def test_clean_alt_questions_drops_blank_duplicate_and_main_question() -> None:
    source = _source(
        type="faq",
        question="加入社团需要交费吗?",
        answer="不收费。",
        alt_questions=("入社要钱吗", " ", "加入社团 需要交费吗?", "入社 要钱吗", "社团收会费吗"),
    )
    assert store.clean_alt_questions(source) == ["入社要钱吗", "社团收会费吗"]


async def test_ingest_faq_with_alt_questions_writes_one_chunk_per_phrasing(monkeypatch) -> None:
    conn = _mock_conn()
    _patch_write(monkeypatch, conn)
    embedded: list[str] = []

    async def embedder(texts):
        embedded.extend(texts)
        return [[0.1] * 4 for _ in texts]

    await store.ingest_source(
        _source(type="faq", question="怎么报名?", answer="官网填表", alt_questions=("在哪报名",)),
        embedder=embedder,
        source_id="kb_faq1",
    )
    assert embedded == ["Q: 怎么报名?\nA: 官网填表", "Q: 在哪报名\nA: 官网填表"]
    faq_sql, faq_params = next(
        c.args for c in conn.execute.call_args_list if "INSERT INTO kb_faq" in c.args[0]
    )
    assert "alt_questions" in faq_sql
    assert faq_params == ("kb_faq1", "怎么报名?", "官网填表", ["在哪报名"])
    chunk_ids = [
        c.args[1][0] for c in conn.execute.call_args_list if "INSERT INTO kb_chunks" in c.args[0]
    ]
    assert chunk_ids == ["kb_faq1#0", "kb_faq1#1"]


def test_get_source_maps_alt_questions() -> None:
    row = {
        "source_id": "kb_1",
        "source_title": "FAQ",
        "source_type": "faq",
        "kind": "normal",
        "tags": [],
        "enabled": True,
        "updated_by": "ops",
    }
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.side_effect = [
        MagicMock(fetchone=lambda: row),
        MagicMock(fetchone=lambda: {"question": "Q", "answer": "A", "alt_questions": ["Q2"]}),
        MagicMock(fetchone=lambda: {"n": 2}),
    ]
    with patch.object(store, "_conn", lambda: conn):
        rec = store.get_source("kb_1")
    assert rec is not None and rec.alt_questions == ["Q2"] and rec.chunk_count == 2


def _faq_record(**over) -> store.SourceRecord:
    base = {
        "source_id": "kb_fee",
        "title": "会费说明",
        "type": "faq",
        "kind": "normal",
        "tags": ["招新"],
        "enabled": True,
        "updated_by": "ops",
        "question": "加入社团需要交费吗?",
        "answer": "不收费。",
        "cycle_id": 12,
        "alt_questions": ["入社要钱吗"],
    }
    return store.SourceRecord(**{**base, **over})


async def test_add_alt_question_appends_and_reingests(monkeypatch) -> None:
    captured: dict = {}

    async def fake_ingest(source, *, embedder=None, source_id=None):
        captured.update(source=source, source_id=source_id)
        return source_id

    monkeypatch.setattr(store, "get_source", lambda sid: _faq_record())
    monkeypatch.setattr(store, "ingest_source", fake_ingest)
    assert await store.add_alt_question("kb_fee", " 社团收会费吗 ", updated_by="user:2") is True
    assert captured["source_id"] == "kb_fee"
    # 其余内容原样保留(含周期绑定),只多一种问法;更新人记为批准的人
    assert captured["source"] == SourceInput(
        title="会费说明",
        type="faq",
        tags=("招新",),
        question="加入社团需要交费吗?",
        answer="不收费。",
        updated_by="user:2",
        cycle_id=12,
        alt_questions=("入社要钱吗", "社团收会费吗"),
    )


@pytest.mark.parametrize("known", ["入社 要钱吗", "加入社团需要交费吗?"])
async def test_add_alt_question_is_idempotent(monkeypatch, known: str) -> None:
    async def never(*_a, **_k):
        raise AssertionError("已有同样的问法时不该重新入库")

    monkeypatch.setattr(store, "get_source", lambda sid: _faq_record())
    monkeypatch.setattr(store, "ingest_source", never)
    assert await store.add_alt_question("kb_fee", known, updated_by="user:2") is False


@pytest.mark.parametrize(
    ("record", "question"),
    [(None, "入社要钱吗"), (_faq_record(type="doc"), "入社要钱吗"), (_faq_record(), "  ")],
)
async def test_add_alt_question_rejects_bad_targets(monkeypatch, record, question: str) -> None:
    monkeypatch.setattr(store, "get_source", lambda sid: record)
    with pytest.raises(KbValidationError):
        await store.add_alt_question("kb_fee", question, updated_by="user:2")


# ── 检索:块 id、同一条 FAQ 只占一个名额 ────────────────────


def _typed_row(source_id: str, score: float, source_type: str, ordinal: int = 0) -> dict:
    return {
        **_row(source_id, score, ordinal=ordinal),
        "source_type": source_type,
        "chunk_id": f"{source_id}#{ordinal}",
    }


def test_fuse_keeps_one_chunk_per_faq_source() -> None:
    rows = [
        _typed_row("kb_faq", 0.90, "faq", ordinal=1),  # 附加问法那一块分最高
        _typed_row("kb_faq", 0.85, "faq", ordinal=0),
        _typed_row("kb_doc", 0.80, "doc", ordinal=0),
        _typed_row("kb_faq", 0.75, "faq", ordinal=2),
        _typed_row("kb_doc", 0.70, "doc", ordinal=3),
    ]
    hits = store._fuse(rows, n_terms=0, top_k=4, min_score=0.45, keyword_min_ratio=0.5)
    # 同一条 FAQ 的三种问法只留最相关的一块;正文条目的各块内容不同,都留
    assert [(h.chunk_id, h.source_type) for h in hits] == [
        ("kb_faq#1", "faq"),
        ("kb_doc#0", "doc"),
        ("kb_doc#3", "doc"),
    ]


def test_fuse_faq_dedupe_frees_slots_for_other_sources() -> None:
    rows = [_typed_row("kb_faq", 0.9 - i * 0.01, "faq", ordinal=i) for i in range(4)]
    rows.append(_typed_row("kb_other", 0.5, "faq"))
    hits = store._fuse(rows, n_terms=0, top_k=2, min_score=0.45, keyword_min_ratio=0.5)
    assert [h.source_id for h in hits] == ["kb_faq", "kb_other"]


def test_fuse_tolerates_rows_without_type_and_chunk_id() -> None:
    hits = store._fuse(
        [_row("kb_a", 0.9), _row("kb_a", 0.8, ordinal=1)],
        n_terms=0,
        top_k=4,
        min_score=0.45,
        keyword_min_ratio=0.5,
    )
    assert len(hits) == 2 and hits[0].chunk_id == "" and hits[0].source_type == ""


def test_search_selects_chunk_id_and_source_type(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[_typed_row("kb_a", 0.87, "doc", ordinal=2)])
    _patch_search(monkeypatch, conn)
    outcome = store._search_sync([0.1, 0.2], top_k=4, include_test=False, query="技术部")
    assert "s.source_type, c.chunk_id" in conn.execute.call_args.args[0]
    assert (outcome.hits[0].chunk_id, outcome.hits[0].source_type) == ("kb_a#2", "doc")


# ── 按块读取 ────────────────────────────────────────────

_CHUNK_ROW = {
    "chunk_id": "kb_a#1",
    "source_id": "kb_a",
    "source_title": "技术部面试",
    "source_type": "doc",
    "cycle_id": 12,
    "chunk_text": "面试在周六。",
    "heading_path": "招新 > 面试",
}


def test_get_chunks_applies_the_same_visibility_as_search(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[_CHUNK_ROW])
    _patch_search(monkeypatch, conn)
    chunks = store.get_chunks(["kb_a#1", "kb_gone#0"], cycle_ids=(12,))
    assert chunks == {
        "kb_a#1": store.ChunkRecord(
            chunk_id="kb_a#1",
            source_id="kb_a",
            title="技术部面试",
            source_type="doc",
            text="面试在周六。",
            heading_path="招新 > 面试",
            cycle_id=12,
        )
    }
    sql, params = conn.execute.call_args.args
    # 停用的、评测用的、向量过期的、不在开放周期内的块一律取不到
    for condition in (
        "s.enabled",
        "s.kind = 'normal'",
        "c.model_version = %(version)s",
        "s.cycle_id IS NULL OR s.cycle_id = ANY(%(cycle_ids)s::int[])",
    ):
        assert condition in sql
    assert params == {"chunk_ids": ["kb_a#1", "kb_gone#0"], "version": 7, "cycle_ids": [12]}


def test_get_chunks_defaults_to_evergreen_only_and_skips_empty_requests(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[])
    _patch_search(monkeypatch, conn)
    assert store.get_chunks(["kb_a#1"]) == {}
    assert conn.execute.call_args.args[1]["cycle_ids"] == []  # 不给周期 = 只认常青条目
    conn.execute.reset_mock()
    assert store.get_chunks([]) == {}
    conn.execute.assert_not_called()


def test_list_source_chunks_in_order_with_cycle_filter(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[_CHUNK_ROW])
    _patch_search(monkeypatch, conn)
    (chunk,) = store.list_source_chunks("kb_a", cycle_ids=(12, 13))
    assert chunk.chunk_id == "kb_a#1" and chunk.heading_path == "招新 > 面试"
    sql, params = conn.execute.call_args.args
    assert "c.source_id = %(source_id)s" in sql and "ORDER BY c.chunk_ordinal" in sql
    assert "s.cycle_id = ANY(%(cycle_ids)s::int[])" in sql
    assert params == {"source_id": "kb_a", "version": 7, "cycle_ids": [12, 13]}


def test_list_faq_questions_flattens_main_and_alt_phrasings(monkeypatch) -> None:
    conn = _mock_conn(
        fetchall=[
            {
                "source_id": "kb_fee",
                "question": "加入社团需要交费吗?",
                "alt_questions": ["入社要钱吗"],
            },
            {"source_id": "kb_venue", "question": "面试在哪里?", "alt_questions": None},
        ]
    )
    _patch_search(monkeypatch, conn)
    assert store.list_faq_questions() == [
        ("kb_fee", "加入社团需要交费吗?"),
        ("kb_fee", "入社要钱吗"),
        ("kb_venue", "面试在哪里?"),
    ]
    sql = conn.execute.call_args.args[0]
    assert "s.enabled AND s.kind = 'normal'" in sql  # 停用与评测条目的问法不参与查重
