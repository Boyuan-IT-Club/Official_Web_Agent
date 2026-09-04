"""SEC-03 审计日志单测:mock 连接,验证 SQL 与字段契约。

真实写库由开发机真库验证覆盖。
"""

from unittest.mock import MagicMock, patch

from official_agent.state import audit


def _mock_conn(row: dict | None = None) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.fetchone.return_value = row
    conn.execute.return_value.fetchall.return_value = [row] if row else []
    return conn


def _audit_row() -> dict:
    return {
        "id": 1,
        "thread_id": "cli:u7:abc12345",
        "acting_user_id": 7,
        "channel": "cli",
        "agent": "graphs.assistant:1.2.3",
        "action": {"tool": "assign_interview", "resume_id": 1},
        "decision": "u7:approve",
        "decision_summary": "将 1 号简历分配给 7 号用户",
        "token": "tok-abc",
        "result": "已分配",
        "trace_id": "trace-1",
        "created_at": None,
    }


def test_write_audit_inserts_fields() -> None:
    row = _audit_row()
    conn = _mock_conn(row)
    with patch.object(audit, "_conn", return_value=conn):
        rec = audit.write_audit(
            thread_id="cli:u7:abc12345",
            acting_user_id=7,
            channel="cli",
            agent="graphs.assistant:1.2.3",
            action={"tool": "assign_interview", "resume_id": 1},
            decision="u7:approve",
            decision_summary="将 1 号简历分配给 7 号用户",
            token="tok-abc",
            result="已分配",
            trace_id="trace-1",
        )
    assert rec.id == 1
    assert rec.acting_user_id == 7
    assert rec.channel == "cli"
    assert rec.action == {"tool": "assign_interview", "resume_id": 1}
    sql, params = conn.execute.call_args.args
    assert "INSERT INTO agent_audit_log" in sql
    assert params[5] == "u7:approve"  # decision
    assert params[6] == "将 1 号简历分配给 7 号用户"  # decision_summary
    assert params[7] == "tok-abc"  # token
    assert params[9] == "trace-1"  # trace_id


def test_write_audit_action_is_json() -> None:
    """action 字典必须 JSON 序列化落库(not 裸 dict)。"""
    conn = _mock_conn(_audit_row())
    with patch.object(audit, "_conn", return_value=conn):
        audit.write_audit(
            thread_id="t", acting_user_id=1, channel="cli", agent="a",
            action={"tool": "x"}, decision="u1:approve", result="r", trace_id="tr",
        )
    params = conn.execute.call_args.args[1]
    import json

    assert isinstance(params[4], str)  # action 是 JSON 字符串
    assert json.loads(params[4]) == {"tool": "x"}


def test_list_audit_filters() -> None:
    conn = _mock_conn(_audit_row())
    with patch.object(audit, "_conn", return_value=conn):
        recs = audit.list_audit(actor_user_id=7, thread_id="cli:u7:abc12345", limit=10)
    assert len(recs) == 1
    assert recs[0].decision == "u7:approve"
    sql, params = conn.execute.call_args.args
    assert "acting_user_id = %s" in sql
    assert "thread_id = %s" in sql
    assert params[-1] == 10  # limit


def test_ensure_audit_table_ddl() -> None:
    conn = _mock_conn()
    with patch.object(audit, "_conn", return_value=conn):
        audit.ensure_audit_table()
    sql = conn.execute.call_args.args[0]
    assert "CREATE TABLE IF NOT EXISTS agent_audit_log" in sql
    assert "idx_audit_user" in sql