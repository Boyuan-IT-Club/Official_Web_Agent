"""cycle_retrospective 表:周期复盘报告,一个周期多个版本。

    generating ──→ draft ──→ published ──→ superseded
         └──→ failed

- 生成是后台任务:先占一行(generating)拿到版本号,模型跑完再填正文转 draft;
  失败转 failed 并留下原因。同一周期同时只允许一份在生成
- 进程在生成途中退出会留下一行永远停在 generating 的版本。占号和读取之前都先把
  超时的这种行转成 failed:不清掉,这个周期就生成不了新报告,轮询的人也等不到结果
- 发布的是某一个具体版本,而且只能从 draft 发布;同一周期同时只有一份 published
  (部分唯一索引兜底),发布新版本时旧的转 superseded
- 正文一旦写入就不再改:没有改写草稿或已发布报告的函数,要改就重新生成一版
- 每个版本记下它依据的快照版本、对比基线,以及用到的全部 prompt 版本

表自举每个库每进程一次,在独立事务里提交(同 snapshot_store)。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from official_agent.config import get_settings
from official_agent.state.pools import pooled_conn

STATUS_GENERATING = "generating"
STATUS_DRAFT = "draft"
STATUS_FAILED = "failed"
STATUS_PUBLISHED = "published"
STATUS_SUPERSEDED = "superseded"

# 一次生成最坏是三位分析员并行一轮加撰稿人两轮,每次模型调用上限 180 秒,合计不到
# 10 分钟。超过这个时限还停在 generating 的行,是进程在生成途中退出留下的
STALE_GENERATING_MINUTES = 15

# 咨询锁的命名空间(任取的常数,只要不与别处的咨询锁撞):按周期串行化
# 「占版本号」与「发布」,免得两个请求同时判断「没有在生成的」「没有已发布的」
_LOCK_NAMESPACE = 0x52455452


class ReportInProgressError(RuntimeError):
    """该周期已经有一份报告在生成。"""


class ReportNotPublishableError(RuntimeError):
    """要发布的版本不是草稿(还在生成、生成失败、已发布或已被取代)。"""

    def __init__(self, status: str) -> None:
        super().__init__(f"只有草稿可以发布,该版本当前状态是 {status}")
        self.status = status


@dataclass(frozen=True)
class ReportRecord:
    """一个周期的一版复盘报告。sections 是结构化正文,report_md 是渲染结果。"""

    cycle_id: int
    version: int
    status: str
    metrics_schema: str
    snapshot_version: int
    baseline_cycle_id: int | None
    baseline_snapshot_version: int | None
    sections: dict[str, Any]
    report_md: str
    unverified: list[dict[str, Any]]
    prompt_versions: dict[str, Any]
    usage: dict[str, Any]
    error: str | None
    created_by: int
    created_at: datetime
    finished_at: datetime | None
    published_by: int | None
    published_at: datetime | None


_ready_urls: set[str] = set()
_ready_lock = threading.Lock()

_COLUMNS = (
    "cycle_id, version, status, metrics_schema, snapshot_version, baseline_cycle_id, "
    "baseline_snapshot_version, sections, report_md, unverified, prompt_versions, usage, "
    "error, created_by, created_at, finished_at, published_by, published_at"
)
_LIST_COLUMNS = (
    "cycle_id, version, status, metrics_schema, snapshot_version, baseline_cycle_id, "
    "error, created_by, created_at, finished_at, published_by, published_at"
)


def reset_table_cache() -> None:
    """清空「表已建好」的记忆(测试改库/删表后用;生产不调用)。"""
    with _ready_lock:
        _ready_urls.clear()


def ensure_report_table() -> None:
    """幂等建 cycle_retrospective;每个库每进程只真正执行一次。"""
    url = get_settings().postgres_url
    if url in _ready_urls:
        return
    with _ready_lock:
        if url in _ready_urls:
            return
        with pooled_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cycle_retrospective (
                    id                        bigserial   PRIMARY KEY,
                    cycle_id                  int         NOT NULL,
                    version                   int         NOT NULL,
                    status                    text        NOT NULL DEFAULT 'generating'
                        CHECK (status IN
                            ('generating', 'draft', 'failed', 'published', 'superseded')),
                    metrics_schema            text        NOT NULL,
                    snapshot_version          int         NOT NULL,
                    baseline_cycle_id         int,
                    baseline_snapshot_version int,
                    sections                  jsonb       NOT NULL DEFAULT '{}',
                    report_md                 text        NOT NULL DEFAULT '',
                    unverified                jsonb       NOT NULL DEFAULT '[]',
                    prompt_versions           jsonb       NOT NULL DEFAULT '{}',
                    usage                     jsonb       NOT NULL DEFAULT '{}',
                    error                     text,
                    created_by                int         NOT NULL,
                    created_at                timestamptz NOT NULL DEFAULT now(),
                    finished_at               timestamptz,
                    published_by              int,
                    published_at              timestamptz,
                    UNIQUE (cycle_id, version)
                )
                """
            )
            # 最后一道闸:应用层已按周期加锁,这两个索引保证即使绕过也不会出现
            # 同一周期两份已发布、或两份同时在生成
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_cycle_retrospective_published "
                "ON cycle_retrospective (cycle_id) WHERE status = 'published'"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_cycle_retrospective_generating "
                "ON cycle_retrospective (cycle_id) WHERE status = 'generating'"
            )
        _ready_urls.add(url)


def _lock_cycle(conn: psycopg.Connection[dict[str, Any]], cycle_id: int) -> None:
    """事务级咨询锁:同一周期的占号与发布排队执行,事务结束自动释放。"""
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_NAMESPACE, cycle_id))


def _fail_abandoned(conn: psycopg.Connection[dict[str, Any]], cycle_id: int) -> None:
    """把该周期停在 generating 超过时限的版本转成 failed。"""
    conn.execute(
        """
        UPDATE cycle_retrospective
        SET status = 'failed', error = '生成超时或进程中断', finished_at = now()
        WHERE cycle_id = %s AND status = 'generating'
          AND created_at < now() - make_interval(mins => %s)
        """,
        (cycle_id, STALE_GENERATING_MINUTES),
    )


def start_report(
    *,
    cycle_id: int,
    metrics_schema: str,
    snapshot_version: int,
    baseline_cycle_id: int | None,
    baseline_snapshot_version: int | None,
    created_by: int,
) -> int:
    """占一个新版本(状态 generating),返回版本号。

    Raises:
        ReportInProgressError: 该周期已有一份没超时的报告在生成。
    """
    ensure_report_table()
    with pooled_conn() as conn:
        _lock_cycle(conn, cycle_id)
        _fail_abandoned(conn, cycle_id)
        busy = conn.execute(
            "SELECT 1 FROM cycle_retrospective WHERE cycle_id = %s AND status = 'generating'",
            (cycle_id,),
        ).fetchone()
        if busy is not None:
            raise ReportInProgressError(f"周期 {cycle_id} 已有一份报告正在生成")
        row = conn.execute(
            """
            INSERT INTO cycle_retrospective
                (cycle_id, version, metrics_schema, snapshot_version, baseline_cycle_id,
                 baseline_snapshot_version, created_by)
            SELECT %s, COALESCE(MAX(version), 0) + 1, %s, %s, %s, %s, %s
            FROM cycle_retrospective WHERE cycle_id = %s
            RETURNING version
            """,
            (
                cycle_id,
                metrics_schema,
                snapshot_version,
                baseline_cycle_id,
                baseline_snapshot_version,
                created_by,
                cycle_id,
            ),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"复盘报告占号后未返回行(cycle={cycle_id})")
    return int(row["version"])


def finish_report(
    cycle_id: int,
    version: int,
    *,
    sections: dict[str, Any],
    report_md: str,
    unverified: list[dict[str, Any]],
    prompt_versions: dict[str, Any],
    usage: dict[str, Any],
) -> bool:
    """生成完成:填正文,generating → draft。返回是否真的更新了。

    只认还在 generating 的行:已被判为超时失败的版本,迟到的结果不再写进去。
    """
    ensure_report_table()
    with pooled_conn() as conn:
        cur = conn.execute(
            """
            UPDATE cycle_retrospective
            SET status = 'draft', sections = %s, report_md = %s, unverified = %s,
                prompt_versions = %s, usage = %s, finished_at = now()
            WHERE cycle_id = %s AND version = %s AND status = 'generating'
            """,
            (
                Jsonb(sections),
                report_md,
                Jsonb(unverified),
                Jsonb(prompt_versions),
                Jsonb(usage),
                cycle_id,
                version,
            ),
        )
        return cur.rowcount > 0


def fail_report(cycle_id: int, version: int, error: str) -> bool:
    """生成失败:generating → failed,留下原因摘要。"""
    ensure_report_table()
    with pooled_conn() as conn:
        cur = conn.execute(
            "UPDATE cycle_retrospective SET status = 'failed', error = %s, finished_at = now() "
            "WHERE cycle_id = %s AND version = %s AND status = 'generating'",
            (error[:500], cycle_id, version),
        )
        return cur.rowcount > 0


def publish_report(cycle_id: int, version: int, *, published_by: int) -> ReportRecord:
    """发布一个草稿版本;该周期原先已发布的那版转 superseded。

    Raises:
        LookupError: 没有这个版本。
        ReportNotPublishableError: 该版本不是草稿。
    """
    ensure_report_table()
    with pooled_conn() as conn:
        _lock_cycle(conn, cycle_id)
        current = conn.execute(
            "SELECT status FROM cycle_retrospective WHERE cycle_id = %s AND version = %s",
            (cycle_id, version),
        ).fetchone()
        if current is None:
            raise LookupError(f"周期 {cycle_id} 没有第 {version} 版报告")
        if current["status"] != STATUS_DRAFT:
            raise ReportNotPublishableError(current["status"])
        conn.execute(
            "UPDATE cycle_retrospective SET status = 'superseded' "
            "WHERE cycle_id = %s AND status = 'published'",
            (cycle_id,),
        )
        row = conn.execute(
            f"""
            UPDATE cycle_retrospective
            SET status = 'published', published_by = %s, published_at = now()
            WHERE cycle_id = %s AND version = %s
            RETURNING {_COLUMNS}
            """,  # noqa: S608
            (published_by, cycle_id, version),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"复盘报告发布后未返回行(cycle={cycle_id}, version={version})")
    return _record(row)


def get_report(
    cycle_id: int, *, version: int | None = None, published_only: bool = False
) -> ReportRecord | None:
    """取一版报告;不给 version 取最新一版。published_only 时只在已发布的里找。"""
    ensure_report_table()
    conds = ["cycle_id = %s"]
    params: list[Any] = [cycle_id]
    if version is not None:
        conds.append("version = %s")
        params.append(version)
    if published_only:
        conds.append("status = 'published'")
    with pooled_conn() as conn:
        _fail_abandoned(conn, cycle_id)
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM cycle_retrospective "  # noqa: S608
            f"WHERE {' AND '.join(conds)} ORDER BY version DESC LIMIT 1",
            params,
        ).fetchone()
    return _record(row) if row else None


def list_reports(cycle_id: int, *, published_only: bool = False) -> list[dict[str, Any]]:
    """一个周期的全部版本(新的在前),不含正文。"""
    ensure_report_table()
    where = "cycle_id = %s" + (" AND status = 'published'" if published_only else "")
    with pooled_conn() as conn:
        _fail_abandoned(conn, cycle_id)
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM cycle_retrospective "  # noqa: S608
            f"WHERE {where} ORDER BY version DESC",
            (cycle_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return default if value is None else value


def _record(row: dict[str, Any]) -> ReportRecord:
    return ReportRecord(
        cycle_id=row["cycle_id"],
        version=row["version"],
        status=row["status"],
        metrics_schema=row["metrics_schema"],
        snapshot_version=row["snapshot_version"],
        baseline_cycle_id=row["baseline_cycle_id"],
        baseline_snapshot_version=row["baseline_snapshot_version"],
        sections=_json(row["sections"], {}),
        report_md=row["report_md"],
        unverified=_json(row["unverified"], []),
        prompt_versions=_json(row["prompt_versions"], {}),
        usage=_json(row["usage"], {}),
        error=row["error"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        published_by=row["published_by"],
        published_at=row["published_at"],
    )
