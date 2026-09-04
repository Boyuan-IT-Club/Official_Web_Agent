"""agent_conversation_log 业务表(M6 #110):每轮对话运营数据落库。

决策(#100/#109/#102):
- SSE 每轮结束落一行;存管理面所需字段,不依赖 Langfuse(现状未接)。
- 正常对话存「用户问题原文 + 回复摘要(非全文)」;写入前强制 PII 过滤
  (手机/QQ/学号确定性规则表脱敏;简单版先行,SEC-08 契约对齐后补)。
- 异常/错误行只存 error_code + 元数据,不存对话内容。
- checkpointer(checkpoints 表)是对话原文权威源,本表不当原文查询面。
- token/缓存率/压缩事件等列(#103/#108)后续票加,本表一次建全列。

与 agent_audit_log / agent_threads 同库(agent 自有 PG),同 state/* 先例。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from official_agent.config import get_settings

# PII 确定性规则表(简单版先行,SEC-08 契约对齐后补)。值经测试钉住。
_MASK_RULES: list[tuple[re.Pattern[str], str]] = [
    # 手机号(11 位,1 开头):留前 3 后 4
    (re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    # 身份证(18 位):留前 4 后 4
    (re.compile(r"(?<!\d)(\d{4})\d{10}(\d{4})(?!\d)"), r"\1**********\2"),
    # QQ(5-11 位纯数字,词边界):全掩
    (re.compile(r"(?<!\d)\d{5,11}(?!\d)"), "*****"),
]


def mask_pii(text: str) -> str:
    """确定性 PII 脱敏:手机号留前 3 后 4、身份证留前 4 后 4、QQ 全掩。

    规则表先于 SEC-08 契约的简单版;无匹配原样返回。纯数字串(如「2024」
    年份、会话 id)不受影响——QQ 规则限 5-11 位且词边界。
    """
    masked = text
    for pattern, repl in _MASK_RULES:
        masked = pattern.sub(repl, masked)
    return masked


@dataclass(frozen=True)
class ConversationRecord:
    """agent_conversation_log 一行。"""

    id: int
    thread_id: str
    user_id: int | None
    channel: str
    user_message: str
    reply_summary: str
    tools: list[str]
    duration_ms: int | None
    error_code: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_hit_tokens: int | None
    cache_miss_tokens: int | None
    compress_event: str | None
    created_at: Any


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_conversation_table() -> None:
    """幂等建 agent_conversation_log 表(#110;同 agent_audit_log L-1 自举)。"""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_conversation_log (
                id               bigserial  PRIMARY KEY,
                thread_id        text       NOT NULL,
                user_id          integer,
                channel          text       NOT NULL DEFAULT 'web',
                user_message     text       NOT NULL DEFAULT '',
                reply_summary    text       NOT NULL DEFAULT '',
                tools            jsonb      NOT NULL DEFAULT '[]',
                duration_ms      integer,
                error_code       text,
                input_tokens     integer,
                output_tokens    integer,
                cache_hit_tokens integer,
                cache_miss_tokens integer,
                compress_event   text,
                created_at       timestamptz NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_user
                ON agent_conversation_log (user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_conversation_thread
                ON agent_conversation_log (thread_id, created_at DESC);
            """
        )


def write_conversation(
    *,
    thread_id: str,
    user_id: int | None,
    channel: str = "web",
    user_message: str = "",
    reply_summary: str = "",
    tools: list[str] | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> ConversationRecord:
    """落一行对话运营数据(SSE 每轮结束调用)。

    PII 过滤:user_message / reply_summary 写入前强制 mask_pii。
    错误行(error_code 非空):只存 error_code + 元数据,user_message/reply_summary 置空。
    tools 序列化为 jsonb。
    """
    if error_code:
        # 异常/错误行不存对话内容(决策 #102/#110)
        user_message = ""
        reply_summary = ""
    else:
        user_message = mask_pii(user_message)
        reply_summary = mask_pii(reply_summary)

    with _conn() as conn:
        row = conn.execute(
            """
            INSERT INTO agent_conversation_log
                (thread_id, user_id, channel, user_message, reply_summary,
                tools, duration_ms, error_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, thread_id, user_id, channel, user_message, reply_summary,
                      tools, duration_ms, error_code, input_tokens, output_tokens,
                      cache_hit_tokens, cache_miss_tokens, compress_event, created_at
            """,
            (
                thread_id,
                user_id,
                channel,
                user_message,
                reply_summary,
                Jsonb(tools or []),
                duration_ms,
                error_code,
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError("conversation_log 写失败")
    return _record(row)


def _record(row: dict[str, Any]) -> ConversationRecord:
    tools_raw = row["tools"]
    tools = tools_raw if isinstance(tools_raw, list) else []
    return ConversationRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        user_id=row["user_id"],
        channel=row["channel"],
        user_message=row["user_message"],
        reply_summary=row["reply_summary"],
        tools=tools,
        duration_ms=row["duration_ms"],
        error_code=row["error_code"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_hit_tokens=row["cache_hit_tokens"],
        cache_miss_tokens=row["cache_miss_tokens"],
        compress_event=row["compress_event"],
        created_at=row["created_at"],
    )
