"""M6 #111 agent_config 配置表单测:mock 连接,验证 SQL 与读写契约。

真实写库由开发机真库验证覆盖(同 test_state_conversation.py 纪律)。
"""

from unittest.mock import MagicMock, patch

from official_agent.state import config_store


def _mock_conn(rows: list[dict] | None = None) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    if rows is not None:
        conn.execute.return_value.fetchall.return_value = rows
        conn.execute.return_value.fetchone.return_value = rows[0] if rows else None
    return conn


# ── 建表 ────────────────────────────────────────────────────────────────

def test_ensure_config_table_creates_table() -> None:
    conn = _mock_conn()
    with patch.object(config_store, "_conn", return_value=conn):
        config_store.ensure_config_table()
    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS agent_config" in c for c in calls)
    # upsert(ON CONFLICT)在 set_config 的 INSERT,不在建表语句
    assert not any("ON CONFLICT" in c for c in calls)


# ── 读写 ────────────────────────────────────────────────────────────────

def test_get_all_config_returns_rows() -> None:
    rows = [
        {"key": "model_strong", "value": "deepseek-v4-flash", "updated_at": None},
        {"key": "llm_provider", "value": "openai-compatible", "updated_at": None},
    ]
    conn = _mock_conn(rows)
    with patch.object(config_store, "_conn", return_value=conn):
        result = config_store.get_all_config()
    assert result == {"model_strong": "deepseek-v4-flash", "llm_provider": "openai-compatible"}

def test_set_config_upserts() -> None:
    conn = _mock_conn()
    with patch.object(config_store, "_conn", return_value=conn):
        config_store.set_config("model_strong", "claude-sonnet-5")
    params = conn.execute.call_args.args[1]
    assert "model_strong" in params
    assert "claude-sonnet-5" in params


def test_delete_config_removes_row() -> None:
    conn = _mock_conn()
    with patch.object(config_store, "_conn", return_value=conn):
        config_store.delete_config("model_light")
    sql = str(conn.execute.call_args.args[0])
    assert "DELETE FROM agent_config" in sql
    assert "model_light" in conn.execute.call_args.args[1]