"""长期记忆数据面:agent_memory 表——按用户隔离的偏好与事实。

一条记忆是一句话(如「主要负责技术部的面试」「希望回答先给结论」),带向量
用于按当前问题召回。设计约束:
- **按用户隔离**:所有读写都带 owner_user_id,不存在跨用户读的入口;
- **向量可缺**:embedding 未配置或调用失败时照常存(embedding 为空),召回退化为
  「最近更新的几条」——记忆不能因为向量服务挂了就整体失效;
- **不混模型**:向量列不定维度,另记 embed_model,召回只比对同模型的向量;
- **有保质期**:超过 memory_ttl_days 没再被写到的记忆不再召回,并由定期任务清理;
- 来源会话(source_thread)留痕:用户删除某个会话时,连同它产生的记忆一起删。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from official_agent.config import get_settings
from official_agent.state.pools import pooled_conn

#: 每个用户最多保留的记忆条数:超出时淘汰最久没更新的,防止无限增长
MAX_PER_USER = 50


@dataclass(frozen=True)
class Memory:
    memory_id: str
    owner_user_id: int
    content: str
    kind: str  # preference | fact
    source_thread: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


def ensure_memory_table() -> None:
    """幂等建表(启动自举 + 首次使用兜底)。向量列不定维度:不同 embedding 模型共存。"""
    with pooled_conn() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory (
                memory_id     text        NOT NULL PRIMARY KEY,
                owner_user_id bigint      NOT NULL,
                content       text        NOT NULL,
                kind          text        NOT NULL CHECK (kind IN ('preference', 'fact')),
                source_thread text        NOT NULL DEFAULT '',
                embedding     vector,
                embed_model   text        NOT NULL DEFAULT '',
                created_at    timestamptz NOT NULL DEFAULT now(),
                updated_at    timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_memory_owner "
            "ON agent_memory (owner_user_id, updated_at DESC)"
        )


_ready_lock = threading.Lock()
_is_ready = False


def _ready() -> None:
    """每进程建一次表(DDL 即使 no-op 也持表锁,不进每次读写)。"""
    global _is_ready
    if _is_ready:
        return
    with _ready_lock:
        if not _is_ready:
            ensure_memory_table()
            _is_ready = True


def mark_ready() -> None:
    """启动自举已建表时置位(lifespan 调用)。"""
    global _is_ready
    _is_ready = True


def _row(r: dict[str, Any]) -> Memory:
    return Memory(
        memory_id=r["memory_id"],
        owner_user_id=int(r["owner_user_id"]),
        content=r["content"],
        kind=r["kind"],
        source_thread=r.get("source_thread") or "",
        created_at=r.get("created_at"),
        updated_at=r.get("updated_at"),
    )


def _vector_literal(vector: list[float] | None) -> str | None:
    return None if vector is None else "[" + ",".join(str(float(x)) for x in vector) + "]"


def list_memories(owner_user_id: int) -> list[Memory]:
    """本人全部记忆(管理页面用),最近更新在前。"""
    _ready()
    with pooled_conn() as conn:
        rows = conn.execute(
            "SELECT memory_id, owner_user_id, content, kind, source_thread, created_at, updated_at "
            "FROM agent_memory WHERE owner_user_id = %s ORDER BY updated_at DESC",
            (owner_user_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def recall(owner_user_id: int, query_vector: list[float] | None, *, limit: int = 3) -> list[str]:
    """按当前问题召回最相关的几条;没有问题向量时取最近更新的几条。

    只看保质期内的记忆;向量比对限同一 embedding 模型。
    """
    _ready()
    settings = get_settings()
    ttl = max(int(settings.memory_ttl_days), 1)
    with pooled_conn() as conn:
        if query_vector is not None:
            rows = conn.execute(
                """
                SELECT content FROM agent_memory
                WHERE owner_user_id = %s AND updated_at > now() - make_interval(days => %s)
                  AND embedding IS NOT NULL AND embed_model = %s
                ORDER BY embedding <=> %s::vector, updated_at DESC
                LIMIT %s
                """,
                (owner_user_id, ttl, settings.embed_model, _vector_literal(query_vector), limit),
            ).fetchall()
            if rows:
                return [r["content"] for r in rows]
        rows = conn.execute(
            """
            SELECT content FROM agent_memory
            WHERE owner_user_id = %s AND updated_at > now() - make_interval(days => %s)
            ORDER BY updated_at DESC LIMIT %s
            """,
            (owner_user_id, ttl, limit),
        ).fetchall()
    return [r["content"] for r in rows]


def apply_changes(
    owner_user_id: int,
    *,
    source_thread: str,
    add: list[tuple[str, str, list[float] | None]],
    update: list[tuple[str, str, list[float] | None]],
    delete: list[str],
) -> int:
    """一次提取的结果落库(单事务):新增 (内容, 类型, 向量)、改写 (id, 内容, 向量)、删除 id。

    改写/删除都带 owner 条件:提取器给出的 id 若不属于本人,静默不生效。
    超出 MAX_PER_USER 时淘汰最久未更新的。返回实际变更行数。
    """
    _ready()
    model = get_settings().embed_model
    changed = 0
    with pooled_conn() as conn:
        for content, kind, vector in add:
            conn.execute(
                """
                INSERT INTO agent_memory
                    (memory_id, owner_user_id, content, kind, source_thread, embedding, embed_model)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                """,
                (
                    f"mem_{secrets.token_hex(6)}",
                    owner_user_id,
                    content,
                    kind,
                    source_thread,
                    _vector_literal(vector),
                    model if vector is not None else "",
                ),
            )
            changed += 1
        for memory_id, content, vector in update:
            cur = conn.execute(
                """
                UPDATE agent_memory
                SET content = %s, embedding = %s::vector, embed_model = %s,
                    source_thread = %s, updated_at = now()
                WHERE memory_id = %s AND owner_user_id = %s
                """,
                (
                    content,
                    _vector_literal(vector),
                    model if vector is not None else "",
                    source_thread,
                    memory_id,
                    owner_user_id,
                ),
            )
            changed += cur.rowcount
        for memory_id in delete:
            cur = conn.execute(
                "DELETE FROM agent_memory WHERE memory_id = %s AND owner_user_id = %s",
                (memory_id, owner_user_id),
            )
            changed += cur.rowcount
        conn.execute(
            """
            DELETE FROM agent_memory WHERE owner_user_id = %s AND memory_id NOT IN (
                SELECT memory_id FROM agent_memory WHERE owner_user_id = %s
                ORDER BY updated_at DESC LIMIT %s
            )
            """,
            (owner_user_id, owner_user_id, MAX_PER_USER),
        )
    return changed


def delete_memory(owner_user_id: int, memory_id: str) -> bool:
    _ready()
    with pooled_conn() as conn:
        cur = conn.execute(
            "DELETE FROM agent_memory WHERE memory_id = %s AND owner_user_id = %s",
            (memory_id, owner_user_id),
        )
        return cur.rowcount > 0


def delete_all(owner_user_id: int) -> int:
    _ready()
    with pooled_conn() as conn:
        return conn.execute(
            "DELETE FROM agent_memory WHERE owner_user_id = %s", (owner_user_id,)
        ).rowcount


def delete_by_thread(owner_user_id: int, source_thread: str) -> int:
    """用户删除会话时,连带删除该会话产生的记忆。"""
    _ready()
    with pooled_conn() as conn:
        return conn.execute(
            "DELETE FROM agent_memory WHERE owner_user_id = %s AND source_thread = %s",
            (owner_user_id, source_thread),
        ).rowcount


def purge_expired() -> int:
    """清理超过保质期的记忆(定期任务调用)。"""
    _ready()
    ttl = max(int(get_settings().memory_ttl_days), 1)
    with pooled_conn() as conn:
        return conn.execute(
            "DELETE FROM agent_memory WHERE updated_at < now() - make_interval(days => %s)",
            (ttl,),
        ).rowcount
