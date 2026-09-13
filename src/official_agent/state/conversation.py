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

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from official_agent.config import get_settings
from official_agent.security.pii import mask_pii  # 共享规则表(security/pii.py;re-export 供既有引用)

# PII 确定性规则表已上移 security/pii.py(#115 review P0-3:工具返回层共用)


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
    prefix_hash: str | None
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
        # M6 #113/#114:prefix_hash、compress_event 为增量加列,老库幂等补齐
        # (#110/#113 建的存量表缺 compress_event → INSERT 全失败且被
        # fail-open 吞掉 = 整表静默停写,必须随建表一起补)
        conn.execute("ALTER TABLE agent_conversation_log ADD COLUMN IF NOT EXISTS prefix_hash text")
        conn.execute(
            "ALTER TABLE agent_conversation_log ADD COLUMN IF NOT EXISTS compress_event text"
        )


def prefix_hash(system_prompt: str, tool_names: list[str]) -> str:
    """prefix 稳定性 hash(#113 命中证据):system prompt + 工具名的确定性指纹。

    同输入同值;prompt/工具任一变化 hash 即变——配合响应 cache 字段,
    双证据判断缓存前缀是否真的稳定(命中率可信的前提)。
    """
    import hashlib

    material = f"{system_prompt}\x00{'|'.join(sorted(tool_names))}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def extract_usage(usage_data: dict[str, Any] | None) -> dict[str, int | None]:
    """从 LLM usage 数据提取 token 数(#113,DeepSeek/OpenAI-compatible)。

    usage_data 接受两种形状(由调用方传原始响应 usage 或 usage_metadata):
    - 原始响应 token_usage(dict):DeepSeek 顶层
      prompt_cache_hit_tokens / prompt_cache_miss_tokens 在此(原生字段);
      langchain-openai 转换会丢这两个字段,故必须读原始。
    - langchain usage_metadata(InputTokenDetails):cache 在
      input_token_details.cache_read / cache_creation(OpenAI 规范),
      或 cached_tokens / cache_write_tokens(langchain 映射键)。
    无数据 → 全 None(fail-open)。
    """
    if not usage_data or not isinstance(usage_data, dict):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cache_hit_tokens": None,
            "cache_miss_tokens": None,
        }
    # 原始响应形状:DeepSeek 顶层 cache 字段 / OpenAI prompt_tokens/completion_tokens
    hit = usage_data.get("prompt_cache_hit_tokens")
    miss = usage_data.get("prompt_cache_miss_tokens")
    input_tokens = usage_data.get("input_tokens", usage_data.get("prompt_tokens"))
    output_tokens = usage_data.get("output_tokens", usage_data.get("completion_tokens"))

    # usage_metadata 形状:cache 在 input_token_details
    if hit is None or miss is None:
        details = usage_data.get("input_token_details") or {}
        if hit is None:
            hit = details.get("cache_read", details.get("cached_tokens"))
        if miss is None:
            miss = details.get("cache_creation", details.get("cache_write_tokens"))

    # 兜底推导(#115 实测):DeepSeek 流式 usage_metadata 只报 cache_read,
    # 不报 miss(其 cache_creation 是 Anthropic 语义,恒缺)→ 未命中 =
    # prompt_tokens − 命中数。否则 hit>0/miss=0 会算出假 100% 缓存率。
    if miss is None and hit is not None and input_tokens is not None:
        miss = max(0, input_tokens - hit)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
    }


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
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_hit_tokens: int | None = None,
    cache_miss_tokens: int | None = None,
    prefix_hash: str | None = None,
    compress_event: str | None = None,
) -> ConversationRecord:
    """落一行对话运营数据(SSE 每轮结束调用)。

    PII 过滤:user_message / reply_summary 写入前强制 mask_pii。
    错误行(error_code 非空):只存 error_code + 元数据,user_message/reply_summary 置空。
    tools 序列化为 jsonb;usage 列(#113)由调用方传 extract_usage 结果;
    prefix_hash 为缓存前缀稳定性证据;compress_event(#114)为压缩事件留痕。
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
                tools, duration_ms, error_code, input_tokens, output_tokens,
                cache_hit_tokens, cache_miss_tokens, prefix_hash, compress_event)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, thread_id, user_id, channel, user_message, reply_summary,
                      tools, duration_ms, error_code, input_tokens, output_tokens,
                      cache_hit_tokens, cache_miss_tokens, prefix_hash,
                      compress_event, created_at
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
                input_tokens,
                output_tokens,
                cache_hit_tokens,
                cache_miss_tokens,
                prefix_hash,
                compress_event,
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
        prefix_hash=row.get("prefix_hash"),
        compress_event=row["compress_event"],
        created_at=row["created_at"],
    )


def list_conversations(
    user_id: int | None = None,
    thread_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """运营列表投影(#112):id/thread/user/问题首字/错误码/时间,不含完整消息。

    列表不返回 user_message/reply_summary 全文(轻量,要全文走详情)。
    可按 user_id / thread_id(#115 详情页拉同会话轮次)过滤;
    LIMIT/OFFSET 分页(调用方限上限)。
    用量字段(#115 投影):token/缓存命中数据随行返回,看板级聚合归 #65。
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    sql = (
        "SELECT id, thread_id, user_id, channel, error_code, created_at, "
        "left(user_message, 20) AS user_message_head, "
        "input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens "
        "FROM agent_conversation_log"
    )
    params: list[Any] = []
    conds: list[str] = []
    if user_id is not None:
        conds.append("user_id = %s")
        params.append(user_id)
    if thread_id is not None:
        conds.append("thread_id = %s")
        params.append(thread_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conversation_id: int) -> dict[str, Any] | None:
    """单行详情(#112):含完整 user_message/reply_summary/tools。

    错误行的 user_message/reply_summary 在写入时已剥离(为空),此处原样返回。
    """
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT id, thread_id, user_id, channel, user_message, reply_summary,
                   tools, duration_ms, error_code, input_tokens, output_tokens,
                   cache_hit_tokens, cache_miss_tokens, prefix_hash, created_at
            FROM agent_conversation_log
            WHERE id = %s
            """,
            (conversation_id,),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    tools = result.get("tools")
    result["tools"] = tools if isinstance(tools, list) else []
    return result


def session_overview(thread_ids: list[str]) -> dict[str, dict[str, Any]]:
    """会话级聚合(#115 会话管理):thread_id → {rounds, last_at, preview}。

    preview 取该会话最近一轮的问题首 20 字;无任何落行的会话不出现在结果里
    (调用方以 agent_threads 记录为准,此处仅补充活跃度)。
    """
    if not thread_ids:
        return {}
    with _conn() as conn:
        last_rows = conn.execute(
            """
            SELECT DISTINCT ON (thread_id)
                   thread_id, created_at AS last_at, left(user_message, 20) AS preview
            FROM agent_conversation_log
            WHERE thread_id = ANY(%s)
            ORDER BY thread_id, created_at DESC
            """,
            (thread_ids,),
        ).fetchall()
        count_rows = conn.execute(
            """
            SELECT thread_id, COUNT(*) AS rounds
            FROM agent_conversation_log
            WHERE thread_id = ANY(%s)
            GROUP BY thread_id
            """,
            (thread_ids,),
        ).fetchall()
    rounds = {r["thread_id"]: r["rounds"] for r in count_rows}
    out: dict[str, dict[str, Any]] = {}
    for r in last_rows:
        out[r["thread_id"]] = {
            "rounds": rounds.get(r["thread_id"], 0),
            "last_at": r["last_at"],
            "preview": r["preview"] or "",
        }
    return out


def delete_thread_conversations(thread_id: str) -> int:
    """物理删除某会话的全部对话日志行(#171 删除闭环;预览/摘要虽已脱敏,
    删除语义要求面面俱到)。返回删除行数。"""
    import psycopg

    from official_agent.config import get_settings

    ensure_conversation_table()
    with psycopg.connect(get_settings().postgres_url) as conn:
        cur = conn.execute(
            "DELETE FROM agent_conversation_log WHERE thread_id = %s",
            (thread_id,),
        )
        return max(cur.rowcount, 0)
