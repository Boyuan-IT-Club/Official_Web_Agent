"""组织记忆数据面:org_memory 表——整个社团共享、跨届保留的经验结论。

一条组织记忆是复盘报告里的一条候选教训,经管理员勾选(可改写)之后才存在;
没有「待审」状态的行——候选教训留在报告里,这张表里的每一行都是有人点过头的。

与用户长期记忆(memory/store.py)的区别:
- 归属整个社团,不按用户隔离;可见范围由 audience 决定(admin 只给管理员,
  member 对社员也可见),候选人永远看不到——那道闸在 memory/recall.py
- 只由人写入:勾选、退役、续期都是管理面上的显式动作
- 有保质期:过了 valid_until 不再召回,但行保留——下一份报告会把它列出来,
  让人决定续期还是退役。过期的规矩比没有规矩更糟,所以不自动续
- 记下出处:哪个周期、哪一版报告的第几条教训、依据哪些指标

向量可缺、不混模型的纪律同 memory/store.py:embedding 不可用时照常存,召回退化为
按勾选时间取最近几条。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from official_agent.config import get_settings
from official_agent.state.pools import pooled_conn

AUDIENCE_ADMIN = "admin"
AUDIENCE_MEMBER = "member"
AUDIENCES = (AUDIENCE_ADMIN, AUDIENCE_MEMBER)

STATUS_APPROVED = "approved"
STATUS_RETIRED = "retired"


class AlreadyApprovedError(RuntimeError):
    """这条候选教训已经被勾选过(还在生效或已过期待续),不重复入库。"""


@dataclass(frozen=True)
class OrgMemory:
    memory_id: str
    kind: str  # lesson | norm | calendar
    content: str
    audience: str  # admin | member
    status: str  # approved | retired
    source_cycle: int
    source_report_version: int
    source_lesson_index: int
    evidence: dict[str, Any]
    approved_by: int
    approved_at: datetime
    valid_until: datetime
    retired_by: int | None
    retired_at: datetime | None
    #: 仍是 approved 但已过保质期:不再召回,等人续期或退役
    expired: bool


_ready_urls: set[str] = set()
_ready_lock = threading.Lock()

_COLUMNS = (
    "memory_id, kind, content, audience, status, source_cycle, source_report_version, "
    "source_lesson_index, evidence, approved_by, approved_at, valid_until, retired_by, "
    "retired_at, (status = 'approved' AND valid_until <= now()) AS expired"
)


def reset_table_cache() -> None:
    """清空「表已建好」的记忆(测试改库/删表后用;生产不调用)。"""
    with _ready_lock:
        _ready_urls.clear()


def ensure_org_memory_table() -> None:
    """幂等建 org_memory;每个库每进程只真正执行一次。向量列不定维度。"""
    url = get_settings().postgres_url
    if url in _ready_urls:
        return
    with _ready_lock:
        if url in _ready_urls:
            return
        with pooled_conn() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS org_memory (
                    memory_id             text        PRIMARY KEY,
                    kind                  text        NOT NULL
                        CHECK (kind IN ('lesson', 'norm', 'calendar')),
                    content               text        NOT NULL,
                    audience              text        NOT NULL DEFAULT 'admin'
                        CHECK (audience IN ('admin', 'member')),
                    status                text        NOT NULL DEFAULT 'approved'
                        CHECK (status IN ('approved', 'retired')),
                    source_cycle          int         NOT NULL,
                    source_report_version int         NOT NULL,
                    source_lesson_index   int         NOT NULL,
                    evidence              jsonb       NOT NULL DEFAULT '{}',
                    approved_by           int         NOT NULL,
                    approved_at           timestamptz NOT NULL DEFAULT now(),
                    valid_until           timestamptz NOT NULL,
                    retired_by            int,
                    retired_at            timestamptz,
                    embedding             vector,
                    embed_model           text        NOT NULL DEFAULT ''
                )
                """
            )
            # 同一条候选教训只能有一条生效的组织记忆;退役之后可以重新勾选
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_memory_source ON org_memory "
                "(source_cycle, source_report_version, source_lesson_index) "
                "WHERE status = 'approved'"
            )
        _ready_urls.add(url)


def _vector_literal(vector: list[float] | None) -> str | None:
    return None if vector is None else "[" + ",".join(str(float(x)) for x in vector) + "]"


def approve(
    *,
    kind: str,
    content: str,
    audience: str,
    source_cycle: int,
    source_report_version: int,
    source_lesson_index: int,
    metric_ids: list[str],
    approved_by: int,
    valid_days: int,
    vector: list[float] | None,
) -> OrgMemory:
    """把一条勾选过的教训存成组织记忆,从现在起 valid_days 天内有效。

    Raises:
        AlreadyApprovedError: 这条候选教训已有一条未退役的组织记忆。
    """
    ensure_org_memory_table()
    try:
        with pooled_conn() as conn:
            row = conn.execute(
                f"""
                INSERT INTO org_memory
                    (memory_id, kind, content, audience, source_cycle, source_report_version,
                     source_lesson_index, evidence, approved_by, valid_until, embedding,
                     embed_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        now() + make_interval(days => %s), %s::vector, %s)
                RETURNING {_COLUMNS}
                """,  # noqa: S608
                (
                    f"org_{secrets.token_hex(6)}",
                    kind,
                    content,
                    audience,
                    source_cycle,
                    source_report_version,
                    source_lesson_index,
                    Jsonb({"metric_ids": list(metric_ids)}),
                    approved_by,
                    valid_days,
                    _vector_literal(vector),
                    get_settings().embed_model if vector is not None else "",
                ),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise AlreadyApprovedError("这条候选教训已经勾选过,先退役原来那条才能重新勾选") from exc
    if row is None:
        raise RuntimeError("组织记忆写入后未返回行")
    return _record(row)


def retire(memory_id: str, *, retired_by: int) -> bool:
    """退役一条组织记忆(不再召回,行保留);返回是否真的退役了一条生效的。"""
    ensure_org_memory_table()
    with pooled_conn() as conn:
        cur = conn.execute(
            "UPDATE org_memory SET status = 'retired', retired_by = %s, retired_at = now() "
            "WHERE memory_id = %s AND status = 'approved'",
            (retired_by, memory_id),
        )
        return cur.rowcount > 0


def renew(memory_id: str, *, valid_days: int) -> OrgMemory | None:
    """续期:保质期改为从现在起 valid_days 天。只对未退役的生效;没有这条返回 None。"""
    ensure_org_memory_table()
    with pooled_conn() as conn:
        row = conn.execute(
            f"""
            UPDATE org_memory SET valid_until = now() + make_interval(days => %s)
            WHERE memory_id = %s AND status = 'approved'
            RETURNING {_COLUMNS}
            """,  # noqa: S608
            (valid_days, memory_id),
        ).fetchone()
    return _record(row) if row else None


def get(memory_id: str) -> OrgMemory | None:
    ensure_org_memory_table()
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM org_memory WHERE memory_id = %s",  # noqa: S608
            (memory_id,),
        ).fetchone()
    return _record(row) if row else None


def list_memories(*, status: str | None = None) -> list[OrgMemory]:
    """全部组织记忆(管理面用),最近勾选的在前;status 给了就只列该状态的。"""
    ensure_org_memory_table()
    where, params = ("WHERE status = %s", (status,)) if status else ("", ())
    with pooled_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM org_memory {where} ORDER BY approved_at DESC",  # noqa: S608
            params,
        ).fetchall()
    return [_record(r) for r in rows]


def list_from_other_cycles(cycle_id: int) -> list[OrgMemory]:
    """未退役、且不是出自这个周期的组织记忆(含已过期的),按出处排。

    下一份报告要把它们列出来,问一句「往届的教训今年还成立吗」。
    """
    ensure_org_memory_table()
    with pooled_conn() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM org_memory "  # noqa: S608
            "WHERE status = 'approved' AND source_cycle <> %s "
            "ORDER BY source_cycle, source_report_version, source_lesson_index",
            (cycle_id,),
        ).fetchall()
    return [_record(r) for r in rows]


def recall(
    audiences: tuple[str, ...], query_vector: list[float] | None, *, limit: int
) -> list[str]:
    """按当前问题召回对这些可见范围生效的组织记忆;没有问题向量时取最近勾选的几条。

    只取未退役、保质期内的;向量比对限同一 embedding 模型。audiences 为空直接返回
    空表,不查库。
    """
    if not audiences:
        return []
    ensure_org_memory_table()
    live = "status = 'approved' AND valid_until > now() AND audience = ANY(%s)"
    with pooled_conn() as conn:
        if query_vector is not None:
            rows = conn.execute(
                f"""
                SELECT content FROM org_memory
                WHERE {live} AND embedding IS NOT NULL AND embed_model = %s
                ORDER BY embedding <=> %s::vector, approved_at DESC
                LIMIT %s
                """,  # noqa: S608
                (
                    list(audiences),
                    get_settings().embed_model,
                    _vector_literal(query_vector),
                    limit,
                ),
            ).fetchall()
            if rows:
                return [r["content"] for r in rows]
        rows = conn.execute(
            f"SELECT content FROM org_memory WHERE {live} "  # noqa: S608
            "ORDER BY approved_at DESC LIMIT %s",
            (list(audiences), limit),
        ).fetchall()
    return [r["content"] for r in rows]


def _record(row: dict[str, Any]) -> OrgMemory:
    evidence = row["evidence"]
    return OrgMemory(
        memory_id=row["memory_id"],
        kind=row["kind"],
        content=row["content"],
        audience=row["audience"],
        status=row["status"],
        source_cycle=row["source_cycle"],
        source_report_version=row["source_report_version"],
        source_lesson_index=row["source_lesson_index"],
        evidence=evidence if isinstance(evidence, dict) else {},
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        valid_until=row["valid_until"],
        retired_by=row["retired_by"],
        retired_at=row["retired_at"],
        expired=bool(row["expired"]),
    )
