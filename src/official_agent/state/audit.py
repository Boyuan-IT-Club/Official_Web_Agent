"""写操作审计日志(SEC-03):每笔写操作落库可查。

权威存储:agent 侧 Postgres(ADR-0006 §审计与回溯契约;Langfuse 可删改,
不作权威)。审计行字段契约见 ADR-0006:
  acting_user_id / channel / agent / action / decision / result / trace_id / timestamp

写路径三重闸(ADR-0006):工具装配 → interrupt → 指纹令牌;审计行在
确认执行后写(只有批准后才记录「执行」,拒绝记录「拒绝」决策)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings


@dataclass(frozen=True)
class AuditRecord:
    """agent_audit_log 一行。"""

    id: int
    thread_id: str
    acting_user_id: int
    channel: str
    agent: str
    action: str
    decision: str
    result: str
    trace_id: str
    created_at: Any


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_audit_table() -> None:
    """幂等建 agent_audit_log 表(SEC-03;L-1 同款自举)。"""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_audit_log (
                id              bigserial  PRIMARY KEY,
                thread_id       text       NOT NULL,
                acting_user_id  integer    NOT NULL,
                channel         text       NOT NULL,
                agent           text       NOT NULL,
                action          jsonb      NOT NULL,
                decision        text       NOT NULL,
                result          text       NOT NULL,
                trace_id        text       NOT NULL,
                created_at      timestamptz NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_audit_user
                ON agent_audit_log (acting_user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_thread
                ON agent_audit_log (thread_id, created_at DESC);
            """
        )


def write_audit(
    *,
    thread_id: str,
    acting_user_id: int,
    channel: str,
    agent: str,
    action: dict[str, Any],
    decision: str,
    result: str,
    trace_id: str,
) -> AuditRecord:
    """写一条审计行。action 是操作指纹字典(工具名+参数,与确认令牌同指纹)。

    decision:approve(批准执行)/ reject(拒绝)——interrupt 恢复决策。
    result:执行结果摘要(成功/失败的可行动文案,TOOL-06)。
    trace_id 串 Langfuse(OBS-02)全过程。
    """
    with _conn() as conn:
        row = conn.execute(
            """
            INSERT INTO agent_audit_log
                (thread_id, acting_user_id, channel, agent, action, decision, result, trace_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, thread_id, acting_user_id, channel, agent, action,
                      decision, result, trace_id, created_at
            """,
            (
                thread_id,
                acting_user_id,
                channel,
                agent,
                json.dumps(action, ensure_ascii=False),
                decision,
                result,
                trace_id,
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError("审计写失败")
    return _record(row)


def list_audit(
    actor_user_id: int | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[AuditRecord]:
    """审计列表;可过滤属主 / 线程。仅授权调用方使用(管理面)。"""
    sql = (
        "SELECT id, thread_id, acting_user_id, channel, agent, action, "
        "decision, result, trace_id, created_at FROM agent_audit_log"
    )
    params: list[Any] = []
    conds: list[str] = []
    if actor_user_id is not None:
        conds.append("acting_user_id = %s")
        params.append(actor_user_id)
    if thread_id is not None:
        conds.append("thread_id = %s")
        params.append(thread_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_record(r) for r in rows]


def _record(row: dict[str, Any]) -> AuditRecord:
    action_raw = row["action"]
    # jsonb 可能已解析为 dict(psycopg 依连接),否则 JSON 字符串
    action = action_raw if isinstance(action_raw, dict) else json.loads(action_raw)
    return AuditRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        acting_user_id=row["acting_user_id"],
        channel=row["channel"],
        agent=row["agent"],
        action=action,
        decision=row["decision"],
        result=row["result"],
        trace_id=row["trace_id"],
        created_at=row["created_at"],
    )