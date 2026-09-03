"""MEM-01/SEC-07 agent_threads 建档 CRUD 单测:mock 连接,验证 SQL 与返回记录。

数据面与真库解耦:mock psycopg 连接,断言 SQL 参数正确、记录映射正确。
真库往返由 scripts/verify_mem01.py 覆盖(开发机跑)。
"""

from unittest.mock import MagicMock, patch

from boyuan_agent.state import threads


def _mock_conn(rows: list[dict] | None = None) -> MagicMock:
    """返回 mock 连接(cur.execute 应答 rows)。支持 with _conn() as conn 协议。"""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (lambda: rows[0]) if rows else (lambda: None)
    cur.fetchall.return_value = rows or []
    cur.rowcount = len(rows or [])
    return conn


def _row(tid: str = "cli:u7:1234abcd", subject: str | None = None) -> dict:
    return {
        "thread_id": tid,
        "owner_user_id": 7,
        "channel": "cli",
        "status": "active",
        "subject": subject,
        "created_at": None,
        "deleted_at": None,
    }


def test_new_thread_id_sec07_format() -> None:
    tid = threads.new_thread_id("cli", 7)
    parts = tid.split(":")
    assert len(parts) == 3
    assert parts[0] == "cli"  # channel
    assert parts[1] == "u7"  # 用户标识
    assert len(parts[2]) == 8  # random8 hex
    # 同一用户每次不同(secrets 防碰撞)
    assert threads.new_thread_id("cli", 7) != tid


def test_new_thread_id_channel_and_user() -> None:
    assert threads.new_thread_id("web", 42).startswith("web:u42:")
    assert threads.new_thread_id("feishu", 9).startswith("feishu:u9:")


def test_create_thread_inserts_with_auto_tid() -> None:
    conn = _mock_conn([_row()])
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.create_thread("cli", 7)
    assert rec.thread_id == "cli:u7:1234abcd"
    assert rec.owner_user_id == 7
    assert rec.channel == "cli"
    assert rec.status == "active"
    assert rec.subject is None
    sql = conn.execute.call_args.args[0]
    assert "INSERT INTO agent_threads" in sql
    params = conn.execute.call_args.args[1]
    assert params[3] == "active"  # status


def test_create_thread_stores_subject() -> None:
    conn = _mock_conn([_row(subject="qa-1")])
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.create_thread("cli", 7, subject="qa-1")
    assert rec.subject == "qa-1"
    params = conn.execute.call_args.args[1]
    assert params[4] == "qa-1"  # subject 位置


def test_create_thread_respects_explicit_tid() -> None:
    conn = _mock_conn([_row(tid="custom")])
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.create_thread("cli", 7, thread_id="custom")
    assert rec.thread_id == "custom"


def test_get_thread_returns_none_when_missing() -> None:
    conn = _mock_conn([])
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.get_thread("nope") is None


def test_resolve_thread_owner_match() -> None:
    """属主匹配才返回(SEC-07 恢复路径硬校验)。"""
    conn = _mock_conn([_row(tid="cli:u7:abc")])
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.resolve_thread("cli:u7:abc", 7) is not None
        assert threads.resolve_thread("cli:u7:abc", 8) is None  # 非属主拒绝


def test_find_active_by_subject_returns_recent() -> None:
    row = _row(subject="qa-1")
    conn = _mock_conn([row])
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.find_active_by_subject(7, "cli", "qa-1")
    assert rec is not None and rec.subject == "qa-1"
    sql, params = conn.execute.call_args.args
    assert "owner_user_id = %s" in sql
    assert "subject = %s" in sql
    assert "status = %s" in sql
    assert params[3] == "active"


def test_find_active_by_subject_none_when_missing() -> None:
    conn = _mock_conn([])
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.find_active_by_subject(7, "cli", "nope") is None


def test_list_active_threads_filters_by_owner() -> None:
    conn = _mock_conn([_row()])
    with patch.object(threads, "_conn", return_value=conn):
        recs = threads.list_active_threads(owner_user_id=7)
    assert len(recs) == 1
    assert recs[0].owner_user_id == 7
    sql, params = conn.execute.call_args.args
    assert "owner_user_id = %s" in sql
    assert params[1] == 7


def test_soft_delete_returns_false_when_not_found() -> None:
    conn = _mock_conn([])  # rowcount=0 → False
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.soft_delete_thread("nope", owner_user_id=7) is False


def test_soft_delete_requires_owner_match() -> None:
    conn = _mock_conn([{"dummy": 1}])  # rowcount=1 → True
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.soft_delete_thread("t1", owner_user_id=7) is True
    sql, params = conn.execute.call_args.args
    assert "owner_user_id = %s" in sql
    assert params[2] == 7  # status, tid, owner


def test_create_thread_same_tid_conflict_returns_existing() -> None:
    """H-3 回归:显式 tid 二次建档不抛 UniqueViolation,返回既有记录。"""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    existing = _row(tid="cli:u7:abc12345")
    conn.execute.side_effect = [
        MagicMock(fetchone=lambda: None),  # INSERT ON CONFLICT → None
        MagicMock(fetchone=lambda: existing),  # SELECT → 既有行
    ]
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.create_thread("cli", 7, thread_id="cli:u7:abc12345")
    assert rec.thread_id == "cli:u7:abc12345"
    assert rec.owner_user_id == 7
    sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("ON CONFLICT" in s for s in sqls)
    assert any(s.startswith("SELECT") for s in sqls)