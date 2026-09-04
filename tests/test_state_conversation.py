"""M6 #110 conversation_log 数据层单测:mock 连接,验证 SQL 与字段契约 + PII 过滤。

真实写库由开发机真库验证覆盖(同 test_state_audit.py 纪律)。
"""

from unittest.mock import MagicMock, patch

from official_agent.state import conversation


def _mock_conn(row: dict | None = None) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    conn.execute.return_value.fetchone.return_value = row
    return conn


def _conversation_row() -> dict:
    return {
        "id": 1,
        "thread_id": "web:u7:8f3a9c2b",
        "user_id": 7,
        "channel": "web",
        "user_message": "我的面试时间是什么时候",
        "reply_summary": "你的面试安排在周六 10:00。",
        "tools": ["get_my_interview"],
        "duration_ms": 1234,
        "error_code": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "compress_event": None,
        "created_at": None,
    }


# ── PII 过滤 ────────────────────────────────────────────────────────────

def test_mask_pii_masks_phone() -> None:
    assert conversation.mask_pii("联系我 13812345678 谢谢") == "联系我 138****5678 谢谢"


def test_mask_pii_masks_qq() -> None:
    assert conversation.mask_pii("我的 QQ 是 123456789") == "我的 QQ 是 *****"


def test_mask_pii_masks_id_card() -> None:
    assert conversation.mask_pii("身份证 310101199001011234") == "身份证 3101**********1234"


def test_mask_pii_leaves_plain_text() -> None:
    text = "面试时间是什么时候"
    assert conversation.mask_pii(text) == text


# ── 建表 ────────────────────────────────────────────────────────────────

def test_ensure_conversation_table_creates_table() -> None:
    conn = _mock_conn()
    with patch.object(conversation, "_conn", return_value=conn):
        conversation.ensure_conversation_table()
    calls = [str(c) for c in conn.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS agent_conversation_log" in c for c in calls)
    assert any("idx_conversation_user" in c for c in calls)
    assert any("idx_conversation_thread" in c for c in calls)


# ── 写入 ────────────────────────────────────────────────────────────────

def test_write_conversation_inserts_fields() -> None:
    row = _conversation_row()
    conn = _mock_conn(row)
    with patch.object(conversation, "_conn", return_value=conn):
        rec = conversation.write_conversation(
            thread_id="web:u7:8f3a9c2b",
            user_id=7,
            channel="web",
            user_message="我的面试时间是什么时候",
            reply_summary="你的面试安排在周六 10:00。",
            tools=["get_my_interview"],
            duration_ms=1234,
        )
    assert rec.thread_id == "web:u7:8f3a9c2b"
    assert rec.user_id == 7
    assert rec.user_message == "我的面试时间是什么时候"
    assert rec.tools == ["get_my_interview"]
    assert rec.duration_ms == 1234
    assert rec.error_code is None


def test_write_conversation_masks_pii_before_insert() -> None:
    conn = _mock_conn(_conversation_row())
    with patch.object(conversation, "_conn", return_value=conn):
        conversation.write_conversation(
            thread_id="web:u7:8f3a9c2b",
            user_id=7,
            channel="web",
            user_message="电话 13812345678",
            reply_summary="已记录 13812345678",
        )
    params = conn.execute.call_args.args[1]
    joined = "|".join(str(p) for p in params)
    assert "138****5678" in joined
    assert "13812345678" not in joined


def test_write_conversation_error_row_strips_content() -> None:
    """异常/错误行只存 error_code + 元数据,不存对话内容(#102/#110 决策)。"""
    conn = _mock_conn(_conversation_row())
    with patch.object(conversation, "_conn", return_value=conn):
        conversation.write_conversation(
            thread_id="web:u7:8f3a9c2b",
            user_id=7,
            channel="web",
            user_message="我的问题",
            reply_summary="有问题的回复",
            error_code="model_error",
            duration_ms=500,
        )
    params = conn.execute.call_args.args[1]
    # 参数顺序: thread_id, user_id, channel, user_message, reply_summary, tools, duration_ms, error_code
    assert params[3] == ""  # user_message 空(错误行不存内容)
    assert params[4] == ""  # reply_summary 空
    assert params[7] == "model_error"  # error_code 保留