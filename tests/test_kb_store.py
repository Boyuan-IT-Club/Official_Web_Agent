"""RAG #134 R1:KB 数据面单测——mock 连接验证 SQL/参数/映射(threads.py 先例)。

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


def test_search_excludes_test_and_disabled_by_default(monkeypatch) -> None:
    conn = _mock_conn(
        fetchall=[
            {
                "source_id": "kb_a",
                "source_title": "标题",
                "chunk_text": "片段",
                "heading_path": "",
                "score": 0.87,
            }
        ]
    )
    monkeypatch.setattr(store, "_conn", lambda: conn)
    monkeypatch.setattr(
        "official_agent.kb.store.current_embed_target", lambda: ("fake-model", 4)
    )
    monkeypatch.setattr(
        "official_agent.kb.store.ensure_kb_schema",
        lambda conn, *, embed_model, dim: 7,
    )
    hits = store._search_sync([0.1, 0.2], top_k=4, include_test=False)
    assert len(hits) == 1
    assert hits[0].source_id == "kb_a" and hits[0].score == pytest.approx(0.87)
    sql = conn.execute.call_args.args[0]
    assert "kind = 'normal'" in sql  # test 条目隔离
    assert "s.enabled" in sql  # 停用退出生检索
    assert "model_version = %s" in sql  # 禁跨模型混排
    params = conn.execute.call_args.args[1]
    assert params[1] == 7  # version 过滤
    assert params[3] == 4  # top_k


def test_search_include_test_drops_kind_filter(monkeypatch) -> None:
    conn = _mock_conn(fetchall=[])
    monkeypatch.setattr(store, "_conn", lambda: conn)
    monkeypatch.setattr(
        "official_agent.kb.store.current_embed_target", lambda: ("fake-model", 4)
    )
    monkeypatch.setattr(
        "official_agent.kb.store.ensure_kb_schema",
        lambda conn, *, embed_model, dim: 7,
    )
    store._search_sync([0.1], top_k=2, include_test=True)
    sql = conn.execute.call_args.args[0]
    assert "kind = 'normal'" not in sql


def test_to_pgvector_format() -> None:
    assert store._to_pgvector([0.5, -1]) == "[0.5,-1.0]"
