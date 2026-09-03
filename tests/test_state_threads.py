"""MEM-01 agent_threads 建档 CRUD 单测:mock 连接,验证 SQL 与返回记录。

数据面与真库解耦:mock psycopg 连接,断言 SQL 参数正确、记录映射正确。
真库往返由 scripts/verify_mem01.py 覆盖(开发机跑)。
"""

from unittest.mock import MagicMock, patch

import pytest

from boyuan_agent.state import threads


def _mock_conn(rows: list[dict] | None = None) -> MagicMock:
    """返回 mock 连接(cur.execute 应答 rows)。支持 with _conn() as conn 协议。"""
    conn = MagicMock()
    # with conn: __enter__ 返回自身,execute 挂在自身
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (
        (lambda: rows[0]) if rows else (lambda: None)
    )
    cur.fetchall.return_value = rows or []
    cur.rowcount = len(rows or [])
    return conn


def test_new_thread_id_format() -> None:
    tid = threads.new_thread_id("cli", "recruit-qa")
    parts = tid.split(":")
    assert len(parts) == 3
    assert parts[0] == "cli"
    assert parts[1] == "recruit-qa"
    assert len(parts[2]) == 8  # random8 hex
    # 同一 subject 每次不同(secrets)
    assert threads.new_thread_id("cli", "recruit-qa") != tid


def test_new_thread_id_rejects_colon_in_subject() -> None:
    with pytest.raises(ValueError):
        threads.new_thread_id("cli", "bad:subject")


def test_create_thread_inserts_with_auto_tid() -> None:
    conn = _mock_conn(
        [{"thread_id": "cli:a:1234abcd", "owner_user_id": 7, "channel": "cli",
          "status": "active", "created_at": None, "deleted_at": None}]
    )
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.create_thread("cli", "a", owner_user_id=7, channel="cli")
    assert rec.thread_id == "cli:a:1234abcd"
    assert rec.owner_user_id == 7
    assert rec.status == "active"
    # SQL 含 INSERT + active 状态
    sql = conn.execute.call_args.args[0]
    assert "INSERT INTO agent_threads" in sql
    assert conn.execute.call_args.args[1][3] == "active"


def test_create_thread_respects_explicit_tid() -> None:
    conn = _mock_conn(
        [{"thread_id": "custom", "owner_user_id": 7, "channel": "cli",
          "status": "active", "created_at": None, "deleted_at": None}]
    )
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.create_thread("cli", "a", owner_user_id=7, channel="cli",
                                    thread_id="custom")
    assert rec.thread_id == "custom"


def test_get_thread_returns_none_when_missing() -> None:
    conn = _mock_conn([])
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.get_thread("nope") is None


def test_list_active_threads_filters_by_owner() -> None:
    row = {"thread_id": "cli:a:1", "owner_user_id": 7, "channel": "cli",
           "status": "active", "created_at": None, "deleted_at": None}
    conn = _mock_conn([row])
    with patch.object(threads, "_conn", return_value=conn):
        recs = threads.list_active_threads(owner_user_id=7)
    assert len(recs) == 1
    assert recs[0].owner_user_id == 7
    # 带 owner 过滤的 SQL
    sql, params = conn.execute.call_args.args
    assert "owner_user_id = %s" in sql
    assert params[1] == 7


def test_soft_delete_returns_false_when_not_found() -> None:
    conn = _mock_conn([])  # rowcount=0 → False
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.soft_delete_thread("nope", owner_user_id=7) is False


def test_soft_delete_requires_owner_match() -> None:
    conn = _mock_conn([{"dummy": 1}])  # 1 row → rowcount=1 → True
    with patch.object(threads, "_conn", return_value=conn):
        assert threads.soft_delete_thread("t1", owner_user_id=7) is True
    sql, params = conn.execute.call_args.args
    assert "owner_user_id = %s" in sql
    assert params[2] == 7  # status, tid, owner


def test_create_thread_same_tid_conflict_returns_existing() -> None:
    """H-3 回归:显式 tid 二次建档不抛 UniqueViolation,返回既有记录。

    fix:INSERT ... ON CONFLICT (thread_id) DO NOTHING 后 fetchone() 为 None,
    走 SELECT 兜底拿既有记录(幂等续接)。
    """
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    # 第一次 execute(INSERT)返回 None(冲突);第二次 execute(SELECT)返回既有行
    existing = {
        "thread_id": "cli:qa:abc12345",
        "owner_user_id": 7,
        "channel": "cli",
        "status": "active",
        "created_at": None,
        "deleted_at": None,
    }
    conn.execute.side_effect = [
        MagicMock(fetchone=lambda: None),  # INSERT ON CONFLICT → None
        MagicMock(fetchone=lambda: existing),  # SELECT → 既有行
    ]
    with patch.object(threads, "_conn", return_value=conn):
        rec = threads.create_thread("cli", "qa", owner_user_id=7, channel="cli",
                                    thread_id="cli:qa:abc12345")
    assert rec.thread_id == "cli:qa:abc12345"
    assert rec.owner_user_id == 7
    # 两次 SQL:先 INSERT ON CONFLICT,后 SELECT
    sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("ON CONFLICT" in s for s in sqls)
    assert any(s.startswith("SELECT") for s in sqls)