"""kb_ops_run / kb_ops_topic 数据层单测——mock 连接,验证 DDL、参数与建表缓存。

真库往返(约束、级联、事务、advisory lock)在 test_kb_ops_integration.py。
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from official_agent.state import kb_ops
from official_agent.state.kb_ops import TopicRow

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def _mock_conn(rowcount: int = 1) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.rowcount = rowcount
    return conn


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    kb_ops.reset_table_cache()
    yield
    kb_ops.reset_table_cache()


@pytest.fixture
def conn(monkeypatch) -> MagicMock:
    conn = _mock_conn()
    monkeypatch.setattr(kb_ops, "pooled_conn", lambda: conn)
    return conn


def _sqls(conn: MagicMock) -> list[str]:
    return [c.args[0] for c in conn.execute.call_args_list]


def _data_calls(conn: MagicMock) -> list:
    """去掉建表与补列语句后剩下的调用。"""
    return [
        c
        for c in conn.execute.call_args_list
        if "CREATE " not in c.args[0] and "ALTER TABLE" not in c.args[0]
    ]


def _topic(**over) -> TopicRow:
    base = {
        "rank": 1,
        "query_count": 4,
        "thread_count": 3,
        "near_miss_count": 2,
        "best_vec_score": 0.42,
        "first_seen": _T0,
        "last_seen": _T0,
        "sample_query_ids": (11, 12),
        "triage_status": "done",
        "scope": "official_knowledge",
        "topic": "社团会费",
        "canonical_question": "加入社团需要交费吗?",
        "reason": "公开规则",
    }
    return TopicRow(**{**base, **over})


# ── 建表 ────────────────────────────────────────────────


def test_ensure_creates_both_tables_with_constraints(conn) -> None:
    kb_ops.ensure_kb_ops_tables()
    sqls = _sqls(conn)
    run_sql = next(s for s in sqls if "CREATE TABLE IF NOT EXISTS kb_ops_run" in s)
    topic_sql = next(s for s in sqls if "CREATE TABLE IF NOT EXISTS kb_ops_topic" in s)
    assert "CHECK (status IN ('running', 'succeeded', 'failed'))" in run_sql
    # 主题随运行级联删除;同一次运行里 rank 不重复
    assert "REFERENCES kb_ops_run(run_id) ON DELETE CASCADE" in topic_sql
    assert "UNIQUE (run_id, rank)" in topic_sql
    for status in ("done", "failed", "unsafe_samples", "out_of_budget", "not_selected"):
        assert f"'{status}'" in topic_sql
    for scope in ("official_knowledge", "personal_data", "out_of_scope", "chitchat"):
        assert f"'{scope}'" in topic_sql
    # 主题表不存用户问题的文本,只存检索日志行的 id
    assert "sample_query_ids   bigint[]" in topic_sql
    assert any("idx_kb_ops_run_started" in s for s in sqls)


def test_ensure_runs_ddl_once_per_database(conn, monkeypatch) -> None:
    kb_ops.ensure_kb_ops_tables()
    first = conn.execute.call_count
    kb_ops.ensure_kb_ops_tables()
    assert conn.execute.call_count == first

    monkeypatch.setattr(
        kb_ops, "get_settings", lambda: MagicMock(postgres_url="postgresql://other/db")
    )
    kb_ops.ensure_kb_ops_tables()
    assert conn.execute.call_count == 2 * first


def test_ensure_failure_is_retried_next_time(monkeypatch) -> None:
    broken = _mock_conn()
    broken.execute.side_effect = RuntimeError("pg down")
    monkeypatch.setattr(kb_ops, "pooled_conn", lambda: broken)
    with pytest.raises(RuntimeError):
        kb_ops.ensure_kb_ops_tables()

    healthy = _mock_conn()
    monkeypatch.setattr(kb_ops, "pooled_conn", lambda: healthy)
    kb_ops.ensure_kb_ops_tables()
    assert any("CREATE TABLE" in s for s in _sqls(healthy))


# ── 运行锁 ──────────────────────────────────────────────


def _patch_connect(monkeypatch, *, locked: bool | None, error: Exception | None = None):
    lock_conn = MagicMock()
    if error is not None:
        lock_conn.execute.side_effect = error
    else:
        lock_conn.execute.return_value.fetchone.return_value = (locked,)
    connect = MagicMock(return_value=lock_conn)
    monkeypatch.setattr(kb_ops.psycopg, "connect", connect)
    return lock_conn, connect


def test_lock_acquired_returns_the_holding_connection(monkeypatch) -> None:
    lock_conn, connect = _patch_connect(monkeypatch, locked=True)
    assert kb_ops.try_acquire_run_lock() is lock_conn
    # 持锁连接不走连接池,且自动提交:锁是会话级的,不依赖事务
    assert connect.call_args.kwargs == {"autocommit": True}
    sql, params = lock_conn.execute.call_args.args
    assert "pg_try_advisory_lock" in sql and params == ("official_agent.kb_ops.run",)
    lock_conn.close.assert_not_called()


def test_lock_busy_returns_none_and_closes_the_connection(monkeypatch) -> None:
    lock_conn, _ = _patch_connect(monkeypatch, locked=False)
    assert kb_ops.try_acquire_run_lock() is None
    lock_conn.close.assert_called_once()


def test_lock_query_failure_closes_the_connection(monkeypatch) -> None:
    lock_conn, _ = _patch_connect(monkeypatch, locked=None, error=RuntimeError("pg down"))
    with pytest.raises(RuntimeError):
        kb_ops.try_acquire_run_lock()
    lock_conn.close.assert_called_once()


# ── 写入 ────────────────────────────────────────────────


def test_create_run_first_fails_leftover_running_rows(conn) -> None:
    kb_ops.create_run(
        "kbops_a", window_start=_T0, window_end=_T0, triggered_by=2, params={"days": 7}
    )
    stale, insert = _data_calls(conn)
    assert "UPDATE kb_ops_run SET status = 'failed'" in stale.args[0]
    assert "WHERE status = 'running'" in stale.args[0]
    assert stale.args[1] == ("进程中断,运行没有完成",)
    sql, params = insert.args
    assert "INSERT INTO kb_ops_run" in sql and "'running'" in sql
    assert params[:4] == ("kbops_a", _T0, _T0, 2)
    assert params[4].obj == {"days": 7}


def test_finish_run_writes_topics_then_marks_succeeded(conn) -> None:
    kb_ops.finish_run(
        "kbops_a",
        stats={"topics": 2},
        prompt_versions={"triage": "kb_ops_triage/v1"},
        topics=[_topic(), _topic(rank=2, triage_status="failed", scope=None, topic=None)],
    )
    first, second, update = _data_calls(conn)
    sql, params = first.args
    assert "INSERT INTO kb_ops_topic" in sql and sql.count("%s") == len(params)
    assert params == (
        "kbops_a",
        1,
        4,
        3,
        2,
        0.42,
        _T0,
        _T0,
        [11, 12],
        "done",
        "official_knowledge",
        "社团会费",
        "加入社团需要交费吗?",
        "公开规则",
        None,
    )
    assert second.args[1][1] == 2 and second.args[1][9:12] == ("failed", None, None)
    sql, params = update.args
    assert "SET status = 'succeeded'" in sql
    assert params[0].obj == {"topics": 2}
    assert params[1].obj == {"triage": "kb_ops_triage/v1"}
    assert params[2] == "kbops_a"


def test_fail_run_records_the_reason(conn) -> None:
    kb_ops.fail_run("kbops_a", "问题向量化失败")
    sql, params = _data_calls(conn)[-1].args
    assert "SET status = 'failed'" in sql and params == ("问题向量化失败", "kbops_a")


def test_purge_keeps_the_newest_runs(conn) -> None:
    conn.execute.return_value.rowcount = 3
    assert kb_ops.purge_old_runs(8) == 3
    sql, params = _data_calls(conn)[-1].args
    assert "DELETE FROM kb_ops_run" in sql
    assert "ORDER BY started_at DESC, run_id DESC LIMIT %s" in sql
    assert params == (8,)


# ── 读取 ────────────────────────────────────────────────


def test_list_runs_newest_first(conn) -> None:
    conn.execute.return_value.fetchall.return_value = [{"run_id": "kbops_b"}, {"run_id": "kbops_a"}]
    assert kb_ops.list_runs(limit=5) == [{"run_id": "kbops_b"}, {"run_id": "kbops_a"}]
    sql, params = _data_calls(conn)[-1].args
    assert "ORDER BY started_at DESC, run_id DESC LIMIT %s" in sql and params == (5,)


def test_get_run_and_latest_succeeded_return_none_when_absent(conn) -> None:
    conn.execute.return_value.fetchone.return_value = None
    assert kb_ops.get_run("kbops_missing") is None
    assert kb_ops.latest_succeeded_run() is None
    assert "WHERE status = 'succeeded'" in _data_calls(conn)[-1].args[0]


def test_list_topics_is_ordered_by_rank(conn) -> None:
    conn.execute.return_value.fetchall.return_value = [{"rank": 1}, {"rank": 2}]
    assert kb_ops.list_topics("kbops_a") == [{"rank": 1}, {"rank": 2}]
    sql, params = _data_calls(conn)[-1].args
    assert "WHERE t.run_id = %s" in sql and "ORDER BY t.rank" in sql
    # 每个主题带上它产出的草稿 id(没有草稿为空)
    assert "LEFT JOIN kb_draft d ON d.run_id = t.run_id AND d.topic_rank = t.rank" in sql
    assert params == ("kbops_a",)


# ── 草稿 ────────────────────────────────────────────────

from official_agent.state.kb_ops import DraftRow  # noqa: E402


def _draft_row(**over) -> DraftRow:
    base = {
        "topic_rank": 1,
        "kind": "new_faq",
        "topic": "社团会费",
        "question": "加入社团需要交费吗?",
        "answer": "加入社团不收取任何费用。",
        "evidence": [{"sentence": "加入社团不收取任何费用。", "chunk_id": "kb_doc#2"}],
        "target_source_id": None,
        "conflict_with": ("kb_old",),
        "support": {"query_count": 4, "sample_query_ids": [11, 12]},
        "review": {"revised": False},
        "prompt_versions": {"draft": "kb_ops_draft/v1"},
        "cycle_id": 12,
    }
    return DraftRow(**{**base, **over})


def test_ensure_creates_draft_table_and_upgrades_topic_table(conn) -> None:
    kb_ops.ensure_kb_ops_tables()
    sqls = _sqls(conn)
    draft_sql = next(s for s in sqls if "CREATE TABLE IF NOT EXISTS kb_draft" in s)
    assert "CHECK (kind IN ('alt_question', 'new_faq', 'missing'))" in draft_sql
    for status in ("pending", "approved", "rejected", "superseded"):
        assert f"'{status}'" in draft_sql
    assert "UNIQUE (run_id, topic_rank)" in draft_sql
    # 草稿不随运行级联删除:运行只留最近几次,待审草稿要留到有人处理
    assert "REFERENCES" not in draft_sql
    assert any("idx_kb_draft_status" in s for s in sqls)
    # 存量库的主题表补上起草结局一列
    alter = next(s for s in sqls if "ALTER TABLE kb_ops_topic" in s)
    assert "ADD COLUMN IF NOT EXISTS draft_status" in alter


def test_finish_run_supersedes_old_drafts_then_inserts_new_ones(conn) -> None:
    kb_ops.finish_run(
        "kbops_a",
        stats={},
        prompt_versions={},
        topics=[_topic(draft_status="drafted")],
        drafts=[_draft_row(), _draft_row(topic_rank=2, status="rejected")],
        superseded_draft_ids=[7, 9],
    )
    topic, supersede, pending, rejected, update = _data_calls(conn)
    assert topic.args[1][-1] == "drafted"
    sql, params = supersede.args
    # 只顶替仍在待审的:已经有人处理过的旧草稿不动
    assert "SET status = 'superseded'" in sql and "AND status = 'pending'" in sql
    assert params == ([7, 9],)
    sql, params = pending.args
    assert "INSERT INTO kb_draft" in sql and sql.count("%s") == len(params)
    assert params[:6] == (
        "kbops_a",
        1,
        "new_faq",
        "社团会费",
        "加入社团需要交费吗?",
        "加入社团不收取任何费用。",
    )
    assert params[6].obj == [{"sentence": "加入社团不收取任何费用。", "chunk_id": "kb_doc#2"}]
    assert params[7:10] == (None, 12, ["kb_old"])
    assert params[10].obj == {"query_count": 4, "sample_query_ids": [11, 12]}
    assert params[13:] == ("pending", "pending")
    assert rejected.args[1][13:] == ("rejected", "rejected")  # 被代码驳回的直接带上处理时间
    assert "SET status = 'succeeded'" in update.args[0]


def test_finish_run_without_drafts_touches_no_draft_rows(conn) -> None:
    kb_ops.finish_run("kbops_a", stats={}, prompt_versions={}, topics=[_topic()])
    assert not any("kb_draft" in c.args[0] for c in _data_calls(conn))


def test_draft_lock_is_keyed_by_draft_id(monkeypatch) -> None:
    lock_conn, connect = _patch_connect(monkeypatch, locked=True)
    assert kb_ops.try_acquire_draft_lock(42) is lock_conn
    sql, params = lock_conn.execute.call_args.args
    assert "pg_try_advisory_lock" in sql and params == ("official_agent.kb_ops.draft:42",)
    assert connect.call_args.kwargs == {"autocommit": True}

    busy_conn, _ = _patch_connect(monkeypatch, locked=False)
    assert kb_ops.try_acquire_draft_lock(42) is None
    busy_conn.close.assert_called_once()


def test_list_pending_draft_questions(conn) -> None:
    conn.execute.return_value.fetchall.return_value = [
        {"draft_id": 7, "question": "入社要钱吗"},
        {"draft_id": 9, "question": "面试在哪"},
    ]
    assert kb_ops.list_pending_draft_questions() == [(7, "入社要钱吗"), (9, "面试在哪")]
    assert "WHERE status = 'pending'" in _data_calls(conn)[-1].args[0]


def test_list_drafts_filters_and_paginates(conn) -> None:
    conn.execute.return_value.fetchone.return_value = {"n": 31}
    conn.execute.return_value.fetchall.return_value = [{"draft_id": 5}]
    result = kb_ops.list_drafts(status="pending", kind="new_faq", page=2, size=10)
    assert result == {"items": [{"draft_id": 5}], "total": 31, "page": 2, "size": 10}
    count, page = _data_calls(conn)[-2:]
    assert "status = %s AND kind = %s" in count.args[0] and count.args[1] == ["pending", "new_faq"]
    assert "ORDER BY created_at DESC, draft_id DESC LIMIT %s OFFSET %s" in page.args[0]
    assert page.args[1] == ["pending", "new_faq", 10, 10]


def test_list_drafts_without_filters(conn) -> None:
    conn.execute.return_value.fetchone.return_value = {"n": 0}
    conn.execute.return_value.fetchall.return_value = []
    assert kb_ops.list_drafts()["total"] == 0
    assert _data_calls(conn)[-1].args[1] == [20, 0]


def test_get_draft_returns_none_when_absent(conn) -> None:
    conn.execute.return_value.fetchone.return_value = None
    assert kb_ops.get_draft(404) is None


def test_update_and_decide_only_touch_pending_drafts(conn) -> None:
    assert kb_ops.update_draft_content(
        5, question="问?", answer="答。", cycle_id=None, edited_by=2
    )
    sql, params = _data_calls(conn)[-1].args
    assert "WHERE draft_id = %s AND status = 'pending'" in sql
    assert params == ("问?", "答。", None, 2, 5)

    assert kb_ops.decide_draft(5, status="approved", decided_by=2, published_source_id="kb_x")
    sql, params = _data_calls(conn)[-1].args
    assert "WHERE draft_id = %s AND status = 'pending'" in sql and "decided_at = now()" in sql
    assert params == ("approved", 2, "kb_x", 5)

    conn.execute.return_value.rowcount = 0  # 别人先处理了:不覆盖,如实返回
    assert kb_ops.decide_draft(5, status="rejected", decided_by=3) is False
    assert kb_ops.update_draft_content(5, question="a", answer="", cycle_id=1, edited_by=3) is False


def test_purge_decided_drafts_never_deletes_pending_ones(conn) -> None:
    conn.execute.return_value.rowcount = 4
    assert kb_ops.purge_decided_drafts(365) == 4
    sql, params = _data_calls(conn)[-1].args
    assert "DELETE FROM kb_draft WHERE status <> 'pending'" in sql and params == (365,)
