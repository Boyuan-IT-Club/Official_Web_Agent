"""cycle_metrics_snapshot 表:每个招新周期的指标快照,只追加、长期保留。

快照是跨周期对比的唯一基线:对话日志、检索日志这些原料会随会话删除与保留期
清理而消失,快照不会——表里没有 thread_id,不挂在任何会话清理路径上,本模块也
不提供删除或改写的函数。能这样无限期保留,是因为内容只有过了小格子抑制的聚合数
(见 retrospective/guard.py),没有个人数据。

- 同一周期可以重复采集:版本号按周期递增,旧版本保留
- 内容与该周期最新一版完全相同时不落新版本(重复触发是幂等的)
- 表自举每个库每进程一次,在独立事务里提交
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from official_agent.config import get_settings
from official_agent.state.pools import pooled_conn


@dataclass(frozen=True)
class SnapshotRecord:
    """一个周期的一版指标快照。metrics 是快照正文(retrospective.metrics 产出的结构)。"""

    cycle_id: int
    version: int
    metrics_schema: str
    metrics: dict[str, Any]
    content_hash: str
    taken_by: int
    taken_at: datetime


#: 已建好表的库 URL。按 URL 记:集成测试会把 URL 指到临时库。
_ready_urls: set[str] = set()
_ready_lock = threading.Lock()

_COLUMNS = "cycle_id, version, metrics_schema, metrics, content_hash, taken_by, taken_at"


def reset_table_cache() -> None:
    """清空「表已建好」的记忆(测试改库/删表后用;生产不调用)。"""
    with _ready_lock:
        _ready_urls.clear()


def ensure_snapshot_table() -> None:
    """幂等建 cycle_metrics_snapshot;每个库每进程只真正执行一次。

    建表在独立事务里提交:若和随后的写入放同一事务,写入失败回滚会把新建的表
    一起回滚,而这里已经记成「建好了」。
    """
    url = get_settings().postgres_url
    if url in _ready_urls:
        return
    with _ready_lock:
        if url in _ready_urls:
            return
        with pooled_conn() as conn:
            # UNIQUE (cycle_id, version) 同时是「按周期取最新一版」的索引
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cycle_metrics_snapshot (
                    id             bigserial   PRIMARY KEY,
                    cycle_id       int         NOT NULL,
                    version        int         NOT NULL,
                    metrics_schema text        NOT NULL,
                    metrics        jsonb       NOT NULL,
                    content_hash   text        NOT NULL,
                    taken_by       int         NOT NULL,
                    taken_at       timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (cycle_id, version)
                )
                """
            )
        _ready_urls.add(url)


def content_hash(metrics: dict[str, Any]) -> str:
    """快照正文的指纹:键排序后的 JSON 取 sha256,同样的内容同样的值。"""
    canonical = json.dumps(metrics, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def save_snapshot(
    *, cycle_id: int, metrics_schema: str, metrics: dict[str, Any], taken_by: int
) -> tuple[SnapshotRecord, bool]:
    """保存一版快照,返回 (记录, 是否新落了一版)。

    正文与该周期最新一版相同 → 不写,返回已有的那一版和 False。否则版本号取
    现存最大值加一。
    """
    ensure_snapshot_table()
    digest = content_hash(metrics)
    # MAX+1 读改写有并发窗口(两人同时触发):撞唯一键后重读,
    # 这时对方那一版已是最新,内容相同就直接复用
    for attempt in range(2):
        try:
            with pooled_conn() as conn:
                latest = conn.execute(
                    f"SELECT {_COLUMNS} FROM cycle_metrics_snapshot "  # noqa: S608
                    "WHERE cycle_id = %s ORDER BY version DESC LIMIT 1",
                    (cycle_id,),
                ).fetchone()
                if latest is not None and latest["content_hash"] == digest:
                    return _record(latest), False
                row = conn.execute(
                    f"""
                    INSERT INTO cycle_metrics_snapshot
                        (cycle_id, version, metrics_schema, metrics, content_hash, taken_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING {_COLUMNS}
                    """,  # noqa: S608
                    (
                        cycle_id,
                        (latest["version"] if latest else 0) + 1,
                        metrics_schema,
                        Jsonb(metrics),
                        digest,
                        taken_by,
                    ),
                ).fetchone()
            if row is None:
                raise RuntimeError(f"指标快照写入后未返回行(cycle={cycle_id})")
            return _record(row), True
        except psycopg.errors.UniqueViolation:
            if attempt:
                raise
    raise RuntimeError("unreachable")


def get_snapshot(
    cycle_id: int, *, version: int | None = None, metrics_schema: str | None = None
) -> SnapshotRecord | None:
    """取一版快照;不给 version 取最新一版。

    metrics_schema 给了就只在该指标定义版本里找——跨周期对比要拿同一版定义的快照。
    """
    ensure_snapshot_table()
    conds = ["cycle_id = %s"]
    params: list[Any] = [cycle_id]
    if version is not None:
        conds.append("version = %s")
        params.append(version)
    if metrics_schema is not None:
        conds.append("metrics_schema = %s")
        params.append(metrics_schema)
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM cycle_metrics_snapshot "  # noqa: S608
            f"WHERE {' AND '.join(conds)} ORDER BY version DESC LIMIT 1",
            params,
        ).fetchone()
    return _record(row) if row else None


def list_snapshots(cycle_id: int) -> list[dict[str, Any]]:
    """一个周期的全部版本(新的在前),不含指标正文。

    domains 是各数据域的采集状态:选哪一版做基线之前,先看它有没有缺域。
    """
    ensure_snapshot_table()
    with pooled_conn() as conn:
        rows = conn.execute(
            """
            SELECT cycle_id, version, metrics_schema, content_hash, taken_by, taken_at,
                   metrics->'domains' AS domains
            FROM cycle_metrics_snapshot
            WHERE cycle_id = %s ORDER BY version DESC
            """,
            (cycle_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _record(row: dict[str, Any]) -> SnapshotRecord:
    metrics = row["metrics"]
    return SnapshotRecord(
        cycle_id=row["cycle_id"],
        version=row["version"],
        metrics_schema=row["metrics_schema"],
        metrics=metrics if isinstance(metrics, dict) else json.loads(metrics),
        content_hash=row["content_hash"],
        taken_by=row["taken_by"],
        taken_at=row["taken_at"],
    )
