"""agent_threads 建档/软删除(SEC-07 属主载体,MEM-01)。

thread 是 checkpointer 的"业务视图":checkpoints 表只存状态快照(SDK 原样),
agent_threads 记录 thread_id → 属主/渠道/生命周期。SEC-07 的命名/属主/终结
全部在这张表上表达;checkpointer 数据保留至 SEC-06 定 TTL。

thread_id v1 规范(MEM-01 拍板):{module}:{subject}:{random8}
  例:assistant:interview:8f3a9c2b · eval:batch-2026:1d0e7f
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from boyuan_agent.config import get_settings

_STATUS_ACTIVE = "active"
_STATUS_TERMINATED = "terminated"


@dataclass(frozen=True)
class ThreadRecord:
    """agent_threads 一行。"""

    thread_id: str
    owner_user_id: int
    channel: str
    status: str
    created_at: Any  # datetime | None,psycopg 依连接返回
    deleted_at: Any | None


def _conn() -> psycopg.Connection[dict[str, Any]]:
    """同步连接(建档是快速元数据操作,CLI 主循环外的边缘路径)。"""
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_agent_threads_table() -> None:
    """幂等建 agent_threads 表(L-1:DDL 进仓库,新环境可自举)。

    与 agent_threads 的软删除语义一致;SDK checkpointer 表由 get_checkpointer
    的 setup() 建,本表是业务档案,独立于此。
    """
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_threads (
                thread_id     text                        NOT NULL PRIMARY KEY,
                owner_user_id integer                     NOT NULL,
                channel       text                        NOT NULL,
                status        text                        NOT NULL DEFAULT 'active',
                created_at    timestamptz                 NOT NULL DEFAULT now(),
                deleted_at    timestamptz
            );
            CREATE INDEX IF NOT EXISTS idx_agent_threads_owner
                ON agent_threads (owner_user_id) WHERE status = 'active';
            """
        )


def new_thread_id(module: str, subject: str, *, random_chars: int = 8) -> str:
    """按规范生成 thread_id。调用方负责校验 subject 合法 ASCII(无冒号)。"""
    if ":" in subject:
        raise ValueError(f"subject 不能含冒号:{subject!r}")
    return f"{module}:{subject}:{secrets.token_hex(random_chars // 2)}"


def create_thread(
    module: str,
    subject: str,
    owner_user_id: int,
    channel: str,
    *,
    thread_id: str | None = None,
) -> ThreadRecord:
    """建档:INSERT agent_threads,返回完整记录(幂等)。

    thread_id 缺省自动生成({module}:{subject}:{random8});显式传入则用传入值
    (CLI 会话续接:checkpointer 的 config 里 thread_id 必须等于本表 thread_id)。
    显式 tid 已存在时不报错(ON CONFLICT DO NOTHING),返回既有记录——续接
    会话/软删除复活不因唯一约束崩(H-3)。
    """
    tid = thread_id or new_thread_id(module, subject)
    with _conn() as conn:
        row = conn.execute(
            """
            INSERT INTO agent_threads (thread_id, owner_user_id, channel, status)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (thread_id) DO NOTHING
            RETURNING thread_id, owner_user_id, channel, status, created_at, deleted_at
            """,
            (tid, owner_user_id, channel, _STATUS_ACTIVE),
        ).fetchone()
        if row is None:
            # 冲突:已存在,取既有记录返回(幂等)
            row = conn.execute(
                "SELECT thread_id, owner_user_id, channel, status, created_at, deleted_at "
                "FROM agent_threads WHERE thread_id = %s",
                (tid,),
            ).fetchone()
    if row is None:
        raise RuntimeError(f"建档失败:{tid}")
    return _record(row)


def get_thread(thread_id: str) -> ThreadRecord | None:
    """按 thread_id 取记录;不存在返回 None。含已软删除(留给审计)。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT thread_id, owner_user_id, channel, status, created_at, deleted_at "
            "FROM agent_threads WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return _record(row) if row else None


def list_active_threads(owner_user_id: int | None = None) -> list[ThreadRecord]:
    """活动线程列表(软删除之外的)。owner_user_id 给则只列该属主。"""
    sql = (
        "SELECT thread_id, owner_user_id, channel, status, created_at, deleted_at "
        "FROM agent_threads WHERE status = %s"
    )
    params: list[Any] = [_STATUS_ACTIVE]
    if owner_user_id is not None:
        sql += " AND owner_user_id = %s"
        params.append(owner_user_id)
    with _conn() as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC", params).fetchall()
    return [_record(r) for r in rows]


def soft_delete_thread(thread_id: str, *, owner_user_id: int) -> bool:
    """软删除:置 status='terminated' + deleted_at=now()。属主必填(SEC-07)。

    owner_user_id 为必填关键字参数——防调用方漏传导致跨属主误删(M-3)。
    """
    sql = "UPDATE agent_threads SET status = %s, deleted_at = now() WHERE thread_id = %s"
    params: list[Any] = [_STATUS_TERMINATED, thread_id]
    if owner_user_id is not None:
        sql += " AND owner_user_id = %s"
        params.append(owner_user_id)
    with _conn() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount > 0


def _record(row: dict[str, Any]) -> ThreadRecord:
    return ThreadRecord(
        thread_id=row["thread_id"],
        owner_user_id=row["owner_user_id"],
        channel=row["channel"],
        status=row["status"],
        created_at=row["created_at"],
        deleted_at=row["deleted_at"],
    )