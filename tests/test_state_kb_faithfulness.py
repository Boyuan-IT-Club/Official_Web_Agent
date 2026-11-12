"""kb_faithfulness_run / kb_faithfulness_sample 数据层单测——mock 连接,验证 DDL 与 SQL。

真库往返(外键级联、抽样、锁)在 test_kb_faithfulness_integration.py。
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from official_agent.state import kb_faithfulness as state
from official_agent.state.kb_faithfulness import SampleRow

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def _mock_conn(rowcount: int = 1) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.rowcount = rowcount
    return conn


@pytest.fixture(autouse=True)
def _fresh_table_cache(monkeypatch):
    state.reset_table_cache()
    # 样本表依赖的两张表各有自己的自举,这里不关心
    monkeypatch.setattr(state, "ensure_conversation_table", lambda: None)
    monkeypatch.setattr(state, "ensure_query_log_table", lambda: None)
    yield
    state.reset_table_cache()


@pytest.fixture
def conn(monkeypatch) -> MagicMock:
    conn = _mock_conn()
    monkeypatch.setattr(state, "pooled_conn", lambda: conn)
    return conn


def _sqls(conn: MagicMock) -> list[str]:
    return [c.args[0] for c in conn.execute.call_args_list]


def _data_calls(conn: MagicMock) -> list:
    return [c for c in conn.execute.call_args_list if "CREATE " not in c.args[0]]


def test_ensure_creates_tables_with_cascades(conn, monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(state, "ensure_conversation_table", lambda: order.append("conversation"))
    monkeypatch.setattr(state, "ensure_query_log_table", lambda: order.append("query_log"))
    state.ensure_faithfulness_tables()
    assert order == ["conversation", "query_log"]  # 外键指向的表先建
    sqls = _sqls(conn)
    sample_sql = next(s for s in sqls if "CREATE TABLE IF NOT EXISTS kb_faithfulness_sample" in s)
    # 样本随运行删除;也随对话日志行删除——用户删会话时不需要另接一步
    assert "REFERENCES kb_faithfulness_run(run_id) ON DELETE CASCADE" in sample_sql
    assert "REFERENCES agent_conversation_log(id) ON DELETE CASCADE" in sample_sql
    assert "UNIQUE (run_id, conversation_id)" in sample_sql
    for status in ("judged", "failed", "no_evidence", "out_of_budget"):
        assert f"'{status}'" in sample_sql
    assert any("idx_kb_faithfulness_sample_conversation" in s for s in sqls)

    first = conn.execute.call_count
    state.ensure_faithfulness_tables()
    assert conn.execute.call_count == first  # 每个库每进程只建一次


def test_lock_uses_its_own_name(monkeypatch) -> None:
    lock_conn = MagicMock()
    lock_conn.execute.return_value.fetchone.return_value = (True,)
    connect = MagicMock(return_value=lock_conn)
    monkeypatch.setattr(state.psycopg, "connect", connect)
    assert state.try_acquire_run_lock() is lock_conn
    sql, params = lock_conn.execute.call_args.args
    assert "pg_try_advisory_lock" in sql and params == ("official_agent.kb_faithfulness.run",)

    lock_conn.execute.return_value.fetchone.return_value = (False,)
    assert state.try_acquire_run_lock() is None
    lock_conn.close.assert_called_once()


def test_candidates_are_answered_turns_with_a_kb_hit(conn) -> None:
    conn.execute.return_value.fetchone.return_value = {"n": 42}
    assert state.count_candidates(since=_T0, until=_T0) == 42
    sql, params = _data_calls(conn)[-1].args
    for condition in (
        "c.error_code IS NULL",
        "c.reply_summary <> ''",
        "c.trace_id IS NOT NULL",
        "q.trace_id = c.trace_id AND q.thread_id = c.thread_id AND q.status = 'ok'",
        "c.created_at >= %s AND c.created_at < %s",
    ):
        assert condition in sql
    assert params == (_T0, _T0)


def test_sampling_is_seeded_and_merges_hits_per_turn(conn) -> None:
    turns = [
        {
            "conversation_id": 5,
            "thread_id": "t5",
            "trace_id": "aa",
            "user_message": "问",
            "reply_summary": "答",
            "created_at": _T0,
        },
        {
            "conversation_id": 9,
            "thread_id": "t9",
            "trace_id": "bb",
            "user_message": "问2",
            "reply_summary": "答2",
            "created_at": _T0,
        },
    ]
    searches = [
        {
            "trace_id": "aa",
            "thread_id": "t5",
            "hit_source_ids": ["kb_a", "kb_b"],
            "cycle_ids": [12],
        },
        {
            "trace_id": "aa",
            "thread_id": "t5",
            "hit_source_ids": ["kb_b", "kb_c"],
            "cycle_ids": [13],
        },
        # 同一个 trace_id 出现在别的会话里(不该有,但不能串):不算进 t9
        {"trace_id": "bb", "thread_id": "other", "hit_source_ids": ["kb_x"], "cycle_ids": []},
    ]
    conn.execute.return_value.fetchall.side_effect = [turns, searches]
    samples = state.sample_candidates(since=_T0, until=_T0, seed="kbfaith_x", limit=30)

    assert [(s["conversation_id"], s["source_ids"], s["cycle_ids"]) for s in samples] == [
        (5, ["kb_a", "kb_b", "kb_c"], [12, 13]),  # 一轮里多次检索:命中合并去重,保持先后
        (9, [], []),
    ]
    pick, hits = _data_calls(conn)[-2:]
    assert "ORDER BY md5(c.id::text || %s), c.id" in pick.args[0]
    assert pick.args[1] == (_T0, _T0, "kbfaith_x", 30)
    assert hits.args[1] == (["aa", "bb"],)


def test_sampling_with_no_candidates_skips_the_second_query(conn) -> None:
    conn.execute.return_value.fetchall.return_value = []
    assert state.sample_candidates(since=_T0, until=_T0, seed="s", limit=10) == []
    assert len(_data_calls(conn)) == 1


def test_citation_summary_counts_each_verdict(conn) -> None:
    conn.execute.return_value.fetchone.return_value = {
        "clean": 40,
        "unverified": 3,
        "unsearched": 5,
    }
    assert state.summarize_citation_verdicts(since=_T0, until=_T0) == {
        "clean": 40,
        "unverified": 3,
        "unsearched": 5,
    }
    _sql, params = _data_calls(conn)[-1].args
    assert params == (
        "%kb_citation=clean%",
        "%kb_citation=unverified:%",
        "%kb_citation=unsearched:%",
        _T0,
        _T0,
    )


def test_create_run_first_fails_leftover_running_rows(conn) -> None:
    state.create_run(
        "kbfaith_a", window_start=_T0, window_end=_T0, triggered_by=2, params={"days": 7}
    )
    stale, insert = _data_calls(conn)
    assert "SET status = 'failed'" in stale.args[0] and "WHERE status = 'running'" in stale.args[0]
    assert insert.args[1][:4] == ("kbfaith_a", _T0, _T0, 2)


def test_finish_run_skips_samples_whose_conversation_is_gone(conn) -> None:
    sample = SampleRow(
        conversation_id=5,
        status="judged",
        source_ids=("kb_a",),
        claims_total=2,
        supported=1,
        unsupported=1,
        claims=({"text": "说法", "verdict": "supported"},),
    )
    state.finish_run(
        "kbfaith_a",
        stats={"faithfulness": 0.5},
        prompt_versions={"faithfulness": "v1"},
        samples=[sample],
    )
    insert, update = _data_calls(conn)
    sql, params = insert.args
    # 只在对话日志行还在时才插:用户在抽样途中删了会话,那个样本直接没有
    assert "FROM agent_conversation_log c WHERE c.id = %s" in sql
    assert params[0] == "kbfaith_a" and params[1:3] == ("judged", ["kb_a"])
    assert params[3:7] == (2, 1, 1, 0) and params[-1] == 5
    assert params[7].obj == [{"text": "说法", "verdict": "supported"}]
    assert "SET status = 'succeeded'" in update.args[0]
    assert update.args[1][0].obj == {"faithfulness": 0.5}


def test_purge_and_reads(conn) -> None:
    conn.execute.return_value.rowcount = 2
    assert state.purge_old_runs(8) == 2
    assert _data_calls(conn)[-1].args[1] == (8,)

    conn.execute.return_value.fetchone.return_value = None
    assert state.get_run("nope") is None and state.latest_succeeded_run() is None

    conn.execute.return_value.fetchall.return_value = [{"sample_id": 1}]
    assert state.list_samples("kbfaith_a") == [{"sample_id": 1}]
    sql, params = _data_calls(conn)[-1].args
    # 问题与回复从对话日志现读,样本表自己不存
    assert "JOIN agent_conversation_log c ON c.id = s.conversation_id" in sql
    assert "ORDER BY s.contradicted DESC, s.unsupported DESC" in sql and params == ("kbfaith_a",)
