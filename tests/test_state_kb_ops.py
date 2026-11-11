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
    """去掉建表语句后剩下的调用。"""
    return [c for c in conn.execute.call_args_list if "CREATE " not in c.args[0]]


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
    assert "WHERE run_id = %s ORDER BY rank" in sql and params == ("kbops_a",)
