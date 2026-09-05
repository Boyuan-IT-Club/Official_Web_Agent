"""M6 #110 conversation_log 数据层单测:mock 连接,验证 SQL 与字段契约 + PII 过滤。

真实写库由开发机真库验证覆盖(同 test_state_audit.py 纪律)。
"""

import re
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


def test_write_conversation_records_compress_event() -> None:
    """M6 #114:压缩事件(触发轮/token/覆盖)随轮落行。"""
    row = _conversation_row()
    conn = _mock_conn(row)
    with patch.object(conversation, "_conn", return_value=conn):
        conversation.write_conversation(
            thread_id="web:u7:8f3a9c2b",
            user_id=7,
            user_message="继续问",
            reply_summary="继续答",
            compress_event="trigger_tokens=31500;covered=38;kept=13;summary_tokens=812",
        )
    sql = str(conn.execute.call_args.args[0])
    assert "compress_event" in sql
    params = conn.execute.call_args.args[1]
    assert params[-1] == "trigger_tokens=31500;covered=38;kept=13;summary_tokens=812"


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
    # 参数顺序: thread_id, user_id, channel, user_message, reply_summary,
    # tools, duration_ms, error_code
    assert params[3] == ""  # user_message 空(错误行不存内容)
    assert params[4] == ""  # reply_summary 空
    assert params[7] == "model_error"  # error_code 保留


# ── 查询(#112) ──────────────────────────────────────────────────────────

def test_list_conversations_returns_projected_rows() -> None:
    rows = [
        {
            "id": 1, "thread_id": "web:u7:8f3a9c2b", "user_id": 7,
            "channel": "web", "error_code": None, "created_at": None,
            "user_message_head": "我的面试",
        },
        {
            "id": 2, "thread_id": "web:u8:abcd1234", "user_id": 8,
            "channel": "web", "error_code": "model_error", "created_at": None,
            "user_message_head": "",
        },
    ]
    conn = _mock_conn(rows)
    conn.execute.return_value.fetchall.return_value = rows
    with patch.object(conversation, "_conn", return_value=conn):
        result = conversation.list_conversations(limit=10, offset=0)
    assert len(result) == 2
    assert result[0]["user_message_head"] == "我的面试"
    assert result[1]["error_code"] == "model_error"
    # 列表投影不含完整消息内容
    assert "reply_summary" not in result[0]


def test_list_conversations_filters_by_user() -> None:
    conn = _mock_conn([])
    conn.execute.return_value.fetchall.return_value = []
    with patch.object(conversation, "_conn", return_value=conn):
        conversation.list_conversations(user_id=7, limit=10)
    sql = str(conn.execute.call_args.args[0])
    params = conn.execute.call_args.args[1]
    assert "user_id = %s" in sql
    assert 7 in params
    assert "LIMIT %s" in sql
    assert "OFFSET %s" in sql


def test_get_conversation_returns_row() -> None:
    row = _conversation_row()
    conn = _mock_conn(row)
    with patch.object(conversation, "_conn", return_value=conn):
        result = conversation.get_conversation(1)
    assert result is not None
    assert result["thread_id"] == "web:u7:8f3a9c2b"
    assert result["user_message"] == "我的面试时间是什么时候"


def test_get_conversation_missing_returns_none() -> None:
    conn = _mock_conn(None)
    with patch.object(conversation, "_conn", return_value=conn):
        result = conversation.get_conversation(999)
    assert result is None


def test_list_conversations_sql_projects_no_full_message() -> None:
    """列表 SQL 必须投影 user_message_head,绝不裸选 user_message/reply_summary
    (防全文泄漏进运营列表的回归;评审 #112 MINOR)。"""
    conn = _mock_conn([])
    conn.execute.return_value.fetchall.return_value = []
    with patch.object(conversation, "_conn", return_value=conn):
        conversation.list_conversations(limit=10)
    sql = str(conn.execute.call_args.args[0])
    assert "user_message_head" in sql
    assert "left(user_message, 20)" in sql
    # 投影列清单不得含裸全文列
    select_part = sql.split("FROM")[0]
    assert "reply_summary" not in select_part
    # 无 "user_message" 作为独立 SELECT 列(仅作为 left() 参数)
    assert re.search(r"(?<!left\()\buser_message\b(?!, 20\))", select_part) is None


# ── usage(#113) ─────────────────────────────────────────────────────────

def test_extract_usage_from_metadata() -> None:
    """从 usage_metadata 提取 token 数(含 DeepSeek cache 字段)。"""
    usage = conversation.extract_usage(
        {
            "input_tokens": 150,
            "output_tokens": 80,
            "total_tokens": 230,
            "input_token_details": {
                "cache_read": 100,
                "cache_creation": 50,
            },
        }
    )
    assert usage["input_tokens"] == 150
    assert usage["output_tokens"] == 80
    assert usage["cache_hit_tokens"] == 100  # cache_read = hit
    assert usage["cache_miss_tokens"] == 50  # cache_creation = miss


def test_extract_usage_empty() -> None:
    """无 usage → 全 None(不崩)。"""
    assert conversation.extract_usage(None) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
    }


def test_write_conversation_stores_usage() -> None:
    """write 传 usage → INSERT 含 token/cache 列(#113)。"""
    row = _conversation_row()
    row.update(
        input_tokens=150, output_tokens=80,
        cache_hit_tokens=100, cache_miss_tokens=50,
    )
    conn = _mock_conn(row)
    with patch.object(conversation, "_conn", return_value=conn):
        rec = conversation.write_conversation(
            thread_id="web:u7:8f3a9c2b",
            user_id=7,
            user_message="hi",
            input_tokens=150,
            output_tokens=80,
            cache_hit_tokens=100,
            cache_miss_tokens=50,
        )
    params = conn.execute.call_args.args[1]
    joined = "|".join(str(p) for p in params)
    assert "150" in joined and "80" in joined
    assert "100" in joined and "50" in joined
    assert rec.input_tokens == 150
    assert rec.cache_hit_tokens == 100


def test_prefix_stability_hash_is_deterministic() -> None:
    """prefix 稳定性 hash:同输入同值,变输入变值(#113 命中证据)。"""
    from official_agent.state.conversation import prefix_hash

    h1 = prefix_hash("system prompt A", ["tool_a", "tool_b"])
    h2 = prefix_hash("system prompt A", ["tool_a", "tool_b"])
    h3 = prefix_hash("system prompt A", ["tool_a", "tool_c"])
    h4 = prefix_hash("system prompt B", ["tool_a", "tool_b"])
    assert h1 == h2  # 稳定
    assert h1 != h3  # 工具变 → hash 变
    assert h1 != h4  # prompt 变 → hash 变


def test_extract_usage_deepseek_top_level_cache_fields() -> None:
    """DeepSeek 原生顶层 prompt_cache_* 字段(原始 response usage 形状)。"""
    usage = conversation.extract_usage(
        {
            "input_tokens": 200,
            "output_tokens": 60,
            "prompt_cache_hit_tokens": 120,
            "prompt_cache_miss_tokens": 80,
        }
    )
    assert usage["input_tokens"] == 200
    assert usage["cache_hit_tokens"] == 120
    assert usage["cache_miss_tokens"] == 80


def test_extract_usage_langchain_cached_tokens_mapping() -> None:
    """langchain 映射形状:input_token_details.cached_tokens 也认。"""
    usage = conversation.extract_usage(
        {
            "input_tokens": 150,
            "output_tokens": 80,
            "input_token_details": {"cached_tokens": 90, "cache_write_tokens": 60},
        }
    )
    assert usage["cache_hit_tokens"] == 90
    assert usage["cache_miss_tokens"] == 60
