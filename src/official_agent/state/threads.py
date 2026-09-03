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

from official_agent.config import get_settings

_STATUS_ACTIVE = "active"
_STATUS_TERMINATED = "terminated"


@dataclass(frozen=True)
class ThreadRecord:
    """agent_threads 一行。"""

    thread_id: str
    owner_user_id: int
    channel: str
    status: str
    subject: str | None
    created_at: Any  # datetime | None,psycopg 依连接返回
    deleted_at: Any | None


def _conn() -> psycopg.Connection[dict[str, Any]]:
    """同步连接(建档是快速元数据操作,CLI 主循环外的边缘路径)。"""
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_agent_threads_table() -> None:
    """幂等建 agent_threads 表(L-1:DDL 进仓库,新环境可自举)。

    subject 列存会话别名(CLI -s),thread_id 是 SEC-07 规范的
    {channel}:{user}:{random8}。
    """
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_threads (
                thread_id     text                        NOT NULL PRIMARY KEY,
                owner_user_id integer                     NOT NULL,
                channel       text                        NOT NULL,
                status        text                        NOT NULL DEFAULT 'active',
                subject       text,
                created_at    timestamptz                 NOT NULL DEFAULT now(),
                deleted_at    timestamptz
            );
            CREATE INDEX IF NOT EXISTS idx_agent_threads_owner
                ON agent_threads (owner_user_id) WHERE status = 'active';
            """
        )


def new_thread_id(channel: str, owner_user_id: int, *, random_chars: int = 8) -> str:
    """SEC-07 规范生成 thread_id:{channel}:{user}:{random8}。

    channel=cli/web/feishu;owner 段防猜(用户标识)+ random8 防碰撞。
    thread_id 自带属主维度,恢复路径可据此校验。
    """
    return f"{channel}:u{owner_user_id}:{secrets.token_hex(random_chars // 2)}"


def create_thread(
    channel: str,
    owner_user_id: int,
    *,
    subject: str | None = None,
    thread_id: str | None = None,
) -> ThreadRecord:
    """建档:INSERT agent_threads,返回完整记录(幂等)。

    thread_id 缺省按 SEC-07 自动生成({channel}:u{owner}:{random8})。
    subject 存会话别名(CLI -s),不拼进 thread_id。
    显式 tid 已存在时 ON CONFLICT DO NOTHING,返回既有记录(续接/复活幂等,H-3)。
    """
    tid = thread_id or new_thread_id(channel, owner_user_id)
    with _conn() as conn:
        row = conn.execute(
            """
            INSERT INTO agent_threads (thread_id, owner_user_id, channel, status, subject)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (thread_id) DO NOTHING
            RETURNING thread_id, owner_user_id, channel, status, subject, created_at, deleted_at
            """,
            (tid, owner_user_id, channel, _STATUS_ACTIVE, subject),
        ).fetchone()
        if row is None:
            # 冲突:已存在,取既有记录返回(幂等)
            row = conn.execute(
                "SELECT thread_id, owner_user_id, channel, status, subject, created_at, deleted_at "
                "FROM agent_threads WHERE thread_id = %s",
                (tid,),
            ).fetchone()
    if row is None:
        raise RuntimeError(f"建档失败:{tid}")
    return _record(row)


def get_thread(thread_id: str) -> ThreadRecord | None:
    """按 thread_id 取记录;不存在返回 None。不做属主校验(留给调用方按需)。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT thread_id, owner_user_id, channel, status, subject, created_at, deleted_at "
            "FROM agent_threads WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()
    return _record(row) if row else None


def resolve_thread(thread_id: str, actor_user_id: int) -> ThreadRecord | None:
    """恢复/读取路径的属主硬校验入口(SEC-07):非属主返回 None(拒绝)。

    调用方(CLI/GRA 恢复历史前)统一走这里,防可枚举跨会话翻看(PII)。
    """
    rec = get_thread(thread_id)
    if rec is None or rec.owner_user_id != actor_user_id:
        return None
    return rec


def find_active_by_subject(
    owner_user_id: int, channel: str, subject: str
) -> ThreadRecord | None:
    """CLI -s 续接:该用户最近 active 且 subject 匹配的 thread;无则 None。

    subject 是用户侧别名,同别名同用户可续接同一个会话。
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT thread_id, owner_user_id, channel, status, subject, created_at, deleted_at "
            "FROM agent_threads WHERE owner_user_id = %s AND channel = %s "
            "AND subject = %s AND status = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (owner_user_id, channel, subject, _STATUS_ACTIVE),
        ).fetchone()
    return _record(row) if row else None


def list_active_threads(owner_user_id: int | None = None) -> list[ThreadRecord]:
    """活动线程列表(软删除之外的)。owner_user_id 给则只列该属主。"""
    sql = (
        "SELECT thread_id, owner_user_id, channel, status, subject, created_at, deleted_at "
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
        subject=row.get("subject"),
        created_at=row["created_at"],
        deleted_at=row["deleted_at"],
    )