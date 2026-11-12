"""选用题型的数据面:从面试官选过的题里归纳出来的、去标识的提问方式。

- 一行一个题型。归纳出来先是草稿(proposed),人审通过(approved)之后才会被
  出题段拿去当范例;不再用的标成 retired。状态只能往前走,由人来改
- 只存题型文字与聚合计数(被选了几次、涉及几份简历、几位面试官)。来源只记题
  引用 id,不存题文,也不记是哪位面试官
- 与题库两表同库。建表只发生在第一次写草稿的时候(replace_proposed),不进启动
  流程;读路径不建表——表还不存在就当作没有题型。题型范例没打开时没有人写草稿,
  所以也就没有这张表
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings

#: 题型的三种状态,以及允许的状态变更:只能往前走,退不回草稿
STATUSES = ("proposed", "approved", "retired")
_ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    "approved": ("proposed",),
    "retired": ("proposed", "approved"),
}

_bootstrap_lock = threading.Lock()
_bootstrapped = False


@dataclass(frozen=True)
class ProposedExemplar:
    """一条待入库的题型草稿。计数由代码从选题记录里数出来,不取模型的说法。"""

    category: str
    pattern: str
    pick_count: int
    resume_count: int
    interviewer_count: int
    source_refs: tuple[str, ...]


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def _ensure_bootstrapped() -> None:
    """建表每进程只做一次:DDL 即使是 no-op 也要拿表锁,不能放在数据路径上反复跑。"""
    global _bootstrapped
    if _bootstrapped:
        return
    with _bootstrap_lock:
        if _bootstrapped:
            return
        with _conn() as conn:
            ensure_qbank_exemplar_table(conn)
        _bootstrapped = True


def ensure_qbank_exemplar_table(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """建题型表(幂等);调用方管理事务。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qbank_exemplar (
            exemplar_id       bigserial   NOT NULL PRIMARY KEY,
            cycle_id          int         NOT NULL,
            category          text        NOT NULL,
            grade_band        text        NOT NULL DEFAULT '',
            pattern           text        NOT NULL,
            pick_count        int         NOT NULL,
            resume_count      int         NOT NULL,
            interviewer_count int         NOT NULL,
            source_refs       jsonb       NOT NULL DEFAULT '[]',
            status            text        NOT NULL DEFAULT 'proposed'
                              CHECK (status IN ('proposed', 'approved', 'retired')),
            prompt_version    text        NOT NULL DEFAULT '',
            reviewed_by       int,
            reviewed_at       timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qbank_exemplar_status ON qbank_exemplar (status, cycle_id)"
    )


def _table_exists(conn: psycopg.Connection[dict[str, Any]]) -> bool:
    """题型表建过没有。读路径用它代替自举:只读的调用不该顺手做 DDL。"""
    row = conn.execute("SELECT to_regclass('qbank_exemplar') AS relation").fetchone()
    return bool(row and row["relation"])


def _row(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    if isinstance(item.get("source_refs"), str):
        item["source_refs"] = json.loads(item["source_refs"])
    return item


def replace_proposed(
    *, cycle_id: int, prompt_version: str, exemplars: list[ProposedExemplar]
) -> list[int]:
    """把某个周期的草稿整批换成这一次归纳的结果(单事务),返回新草稿的 id。

    只动 proposed 的行:重新归纳一遍不该冲掉已经有人审过的题型。
    """
    _ensure_bootstrapped()
    ids: list[int] = []
    with _conn() as conn:
        conn.execute(
            "DELETE FROM qbank_exemplar WHERE cycle_id = %s AND status = 'proposed'", (cycle_id,)
        )
        for exemplar in exemplars:
            row = conn.execute(
                """
                INSERT INTO qbank_exemplar
                    (cycle_id, category, pattern, pick_count, resume_count,
                     interviewer_count, source_refs, prompt_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING exemplar_id
                """,
                (
                    cycle_id,
                    exemplar.category,
                    exemplar.pattern,
                    exemplar.pick_count,
                    exemplar.resume_count,
                    exemplar.interviewer_count,
                    json.dumps(list(exemplar.source_refs)),
                    prompt_version,
                ),
            ).fetchone()
            ids.append(int(row["exemplar_id"]) if row else 0)
    return ids


def list_exemplars(*, status: str | None = None, cycle_id: int | None = None) -> list[dict]:
    """题型列表(可按状态、来源周期过滤),选用面广的在前;表还没建过返回空列表。"""
    clauses, params = [], []
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    if cycle_id is not None:
        clauses.append("cycle_id = %s")
        params.append(cycle_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn() as conn:
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            "SELECT exemplar_id, cycle_id, category, grade_band, pattern, pick_count, "
            "resume_count, interviewer_count, source_refs, status, prompt_version, "
            "reviewed_by, reviewed_at, created_at "
            # where 只由上面两条固定子句拼成,取值都走参数绑定
            "FROM qbank_exemplar " + where + " "
            "ORDER BY interviewer_count DESC, pick_count DESC, exemplar_id",
            params,
        ).fetchall()
    return [_row(row) for row in rows]


def get_exemplar(exemplar_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        if not _table_exists(conn):
            return None
        row = conn.execute(
            "SELECT exemplar_id, cycle_id, category, pattern, status FROM qbank_exemplar "
            "WHERE exemplar_id = %s",
            (exemplar_id,),
        ).fetchone()
    return dict(row) if row else None


def set_status(exemplar_id: int, status: str, *, reviewed_by: int) -> bool:
    """改一条题型的状态;当前状态不允许这次变更(或没有这条)时返回 False。

    允许的变更只有 proposed→approved、proposed→retired、approved→retired。
    条件写在 UPDATE 里,两个人同时审同一条时只有一个生效。
    """
    allowed_from = _ALLOWED_FROM.get(status)
    if allowed_from is None:
        raise ValueError(f"不支持把题型改成 {status!r}")
    with _conn() as conn:
        if not _table_exists(conn):
            return False
        cursor = conn.execute(
            "UPDATE qbank_exemplar SET status = %s, reviewed_by = %s, reviewed_at = now() "
            "WHERE exemplar_id = %s AND status = ANY(%s)",
            (status, reviewed_by, exemplar_id, list(allowed_from)),
        )
        return cursor.rowcount == 1


def approved_patterns(*, limit: int) -> list[dict[str, Any]]:
    """给出题段用的范例:已通过人审的题型,选用面最广的在前,至多 limit 条。"""
    with _conn() as conn:
        if not _table_exists(conn):
            return []
        rows = conn.execute(
            "SELECT exemplar_id, category, pattern FROM qbank_exemplar "
            "WHERE status = 'approved' "
            "ORDER BY interviewer_count DESC, pick_count DESC, exemplar_id LIMIT %s",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
