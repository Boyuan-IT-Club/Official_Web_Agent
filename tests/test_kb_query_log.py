"""检索日志(kb_query_log)数据层单测——mock 连接,验证 DDL/参数/脱敏/失败语义。

真库往返(数组列、CHECK 约束、按会话删除)在 test_kb_integration.py 集成档。
"""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from official_agent.kb import query_log
from official_agent.kb.query_log import QueryLogEntry


def _mock_conn(rowcount: int = 1) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.rowcount = rowcount
    return conn


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    """「表已建好」按进程记忆;每个用例换了 mock 连接,先清掉。"""
    query_log.reset_table_cache()
    yield
    query_log.reset_table_cache()


@pytest.fixture
def conn(monkeypatch) -> MagicMock:
    conn = _mock_conn()
    monkeypatch.setattr(query_log, "pooled_conn", lambda: conn)
    return conn


def _entry(**over) -> QueryLogEntry:
    base = {
        "thread_id": "web:u7:8f3a9c2b",
        "trace_id": "ab" * 16,
        "query": "技术部做什么",
        "status": query_log.STATUS_OK,
        "best_vec_score": 0.87,
        "best_kw_ratio": 0.5,
        "hit_source_ids": ("kb_a", "kb_b"),
        "cycle_ids": (12,),
    }
    base.update(over)
    return QueryLogEntry(**base)


def _sqls(conn: MagicMock) -> list[str]:
    return [c.args[0] for c in conn.execute.call_args_list]


# ── 建表 ────────────────────────────────────────────────


def test_ensure_creates_table_and_indexes(conn) -> None:
    query_log.ensure_query_log_table()
    sqls = _sqls(conn)
    assert any("CREATE TABLE IF NOT EXISTS kb_query_log" in s for s in sqls)
    table_sql = next(s for s in sqls if "CREATE TABLE" in s)
    # 状态只有三种;写错的状态要在库里就被拒绝
    assert "CHECK (status IN ('ok', 'not_found', 'unavailable'))" in table_sql
    for index in ("idx_kb_query_log_created", "idx_kb_query_log_thread", "idx_kb_query_log_trace"):
        assert any(index in s for s in sqls), index


def test_ensure_runs_ddl_once_per_database(conn, monkeypatch) -> None:
    query_log.ensure_query_log_table()
    first = conn.execute.call_count
    query_log.ensure_query_log_table()
    assert conn.execute.call_count == first  # 第二次不再发任何 DDL

    # 换了库(集成测试指到临时库)要重新建
    monkeypatch.setattr(
        query_log, "get_settings", lambda: MagicMock(postgres_url="postgresql://other/db")
    )
    query_log.ensure_query_log_table()
    assert conn.execute.call_count == 2 * first


def test_ensure_failure_is_retried_next_time(monkeypatch) -> None:
    """建表失败不能被记成「建好了」,否则此后的写入全部失败且无人重试建表。"""
    broken = _mock_conn()
    broken.execute.side_effect = RuntimeError("pg down")
    monkeypatch.setattr(query_log, "pooled_conn", lambda: broken)
    with pytest.raises(RuntimeError):
        query_log.ensure_query_log_table()

    healthy = _mock_conn()
    monkeypatch.setattr(query_log, "pooled_conn", lambda: healthy)
    query_log.ensure_query_log_table()
    assert any("CREATE TABLE" in s for s in _sqls(healthy))


# ── 写入 ────────────────────────────────────────────────


def test_write_inserts_all_signals(conn) -> None:
    query_log.write_query(_entry())
    sql, params = conn.execute.call_args.args
    assert "INSERT INTO kb_query_log" in sql
    assert params == (
        "web:u7:8f3a9c2b",
        "ab" * 16,
        "技术部做什么",
        "ok",
        0.87,
        0.5,
        ["kb_a", "kb_b"],
        [12],
    )
    assert sql.count("%s") == len(params)


def test_write_masks_pii_in_query(conn) -> None:
    """模型改写的问题里可能带着用户报的手机号:落库前必须脱敏。"""
    query_log.write_query(_entry(query="我手机 13812345678,面试能改期吗"))
    params = conn.execute.call_args.args[1]
    assert params[2] == "我手机 138****5678,面试能改期吗"


def test_write_unavailable_row_has_no_scores(conn) -> None:
    query_log.write_query(
        _entry(
            status=query_log.STATUS_UNAVAILABLE,
            best_vec_score=None,
            best_kw_ratio=None,
            hit_source_ids=(),
            cycle_ids=(),
        )
    )
    params = conn.execute.call_args.args[1]
    assert params[3:] == ("unavailable", None, None, [], [])


# ── 后台写入(fail-open)──────────────────────────────────


async def _drain() -> None:
    """等后台写入任务跑完。"""
    await asyncio.gather(*list(query_log._background_tasks))


async def test_record_query_writes_in_background(monkeypatch) -> None:
    written: list[QueryLogEntry] = []
    monkeypatch.setattr(query_log, "write_query", written.append)
    query_log.record_query(_entry())
    assert written == []  # 调用立即返回,写入还没发生
    await _drain()
    assert written == [_entry()]
    assert not query_log._background_tasks  # 完成的任务不留引用


async def test_record_query_swallows_write_failure(monkeypatch, caplog) -> None:
    """写入失败只告警:检索结果已经给了模型,不能因为观测落库失败影响对话。"""

    def _boom(_entry: QueryLogEntry) -> None:
        raise RuntimeError("pg down")

    monkeypatch.setattr(query_log, "write_query", _boom)
    with caplog.at_level(logging.WARNING, logger="official_agent.kb.query_log"):
        query_log.record_query(_entry(status=query_log.STATUS_NOT_FOUND))
        await _drain()  # 不抛
    assert "kb_query_log 写入失败" in caplog.text
    assert "status=not_found" in caplog.text
    assert "技术部做什么" not in caplog.text  # 问题原文不进日志


# ── 删除 ────────────────────────────────────────────────


def test_delete_thread_queries_is_scoped_to_the_thread(monkeypatch) -> None:
    conn = _mock_conn(rowcount=3)
    monkeypatch.setattr(query_log, "pooled_conn", lambda: conn)
    assert query_log.delete_thread_queries("web:u7:8f3a9c2b") == 3
    sql, params = conn.execute.call_args.args
    assert sql == "DELETE FROM kb_query_log WHERE thread_id = %s"
    assert params == ("web:u7:8f3a9c2b",)


def test_delete_thread_queries_propagates_failure(monkeypatch) -> None:
    """删除失败必须抛给调用方——删会话的路由据此报「没删干净」。"""
    conn = _mock_conn()
    conn.execute.side_effect = RuntimeError("pg down")
    monkeypatch.setattr(query_log, "pooled_conn", lambda: conn)
    with pytest.raises(RuntimeError):
        query_log.delete_thread_queries("web:u7:8f3a9c2b")
