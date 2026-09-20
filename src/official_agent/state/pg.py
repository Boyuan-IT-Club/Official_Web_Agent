"""Postgres 状态设施:checkpointer 工厂。

- 与 Langfuse 共享同一实例(ADR-0007),库 official_agent,另建 database 隔离
- AsyncPostgresSaver:CLI/SSE 事件循环上用 async 连接,不阻塞线程池
- get_checkpointer() 上下文管理器:进入建连接池并建表(幂等),退出关池
- agent_threads 建档/软删除在 state/threads.py(属主载体)

注 1:必须用 AsyncConnectionPool 而非 from_conn_string——后者是单连接,
  全服务共享;任一请求在查询中途断连(用户关浏览器)即毒化连接
  (「another command is already in progress」),此后所有请求全挂
  (真实联调中踩到过)。连接池隔离并发与坏连接。
注 2:池连接必须 autocommit=True + prepare_threshold=0(langgraph 官方
  生产部署要求,checkpoint 写路径依赖);row_factory=dict_row 与
  from_conn_string 先例一致。
注 3:AsyncPostgresSaver 的 setup() 被覆写为 async,必须 await(同步调用
  会静默不建表)。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from official_agent.config import get_settings


@asynccontextmanager
async def get_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """Yields 共享 AsyncPostgresSaver(连接池背后),进入时建池并建表(幂等)。

    用法:
        async with get_checkpointer() as saver:
            agent = create_agent(..., checkpointer=saver)
    """
    pool = AsyncConnectionPool(
        conninfo=get_settings().postgres_url,
        max_size=10,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    try:
        await pool.open(wait=True, timeout=15)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()  # 建 SDK 表(幂等)
        yield saver
    finally:
        await pool.close()


def purge_thread_checkpoints(thread_id: str) -> int:
    """物理删除某 thread 的全部 checkpoint 数据(删除闭环)。

    LangGraph PostgresSaver 三表(checkpoints / checkpoint_writes /
    checkpoint_blobs)都按 thread_id 维度;缺表容错(旧库未建全时跳过,
    与 saver.setup() 幂等建表解耦)。返回删除总行数(排障/审计用)。
    """
    import psycopg

    deleted = 0
    with psycopg.connect(get_settings().postgres_url) as conn:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = %s",  # noqa: S608
                    (thread_id,),
                )
                deleted += max(cur.rowcount, 0)
            except psycopg.errors.UndefinedTable:
                conn.rollback()
    return deleted
# ── checkpointer 挂起载荷 24h TTL(ADR-0007 挂起态清理) ──

_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
_INTERRUPT_CHANNEL = "__interrupt__"


def _uuid_timestamp_age_hours(checkpoint_id: str, now: float) -> float | None:
    """从 checkpoint id 解析年龄(小时)。仅接受 RFC9562 v6(langgraph 时间
    有序 id);其他版本/非 UUID → None(宁可不删不可误删)。

    v6 的 60 位 unix 时间戳按 hi/mid/low 重排——py3.12 的 stdlib .time 按
    v1 字段序解码会得到垃圾值(实测解出 3117 年),必须显式重排。"""
    import uuid
    from datetime import datetime

    try:
        u = uuid.UUID(checkpoint_id)
    except ValueError:
        return None
    if u.version != 6:
        return None
    i = u.int
    ts_100ns = ((i >> 96) << 28) | ((i >> 80 & 0xFFFF) << 12) | ((i >> 64) & 0xFFF)
    try:
        created = datetime.fromtimestamp((ts_100ns - 0x01B21DD213814000) / 1e7, tz=UTC)
        return (now - created.timestamp()) / 3600
    except (OSError, OverflowError):
        return None


def purge_expired_interrupts(
    *, max_age_hours: int = 24, conn: Any = None, dsn: str | None = None
) -> int:
    """清理挂起超时的 checkpointer 载荷:require_confirmation 挂起的
    会话超 24h 未恢复 → 删除该 thread 的 checkpoints/blobs/writes 三表行。

    挂起判定:checkpoint_writes 存在 __interrupt__ 通道写入;年龄取该
    thread 最新 checkpoint id 的时间戳(langgraph 时间有序 UUID,解析失败
    跳过——宁可不删不可误删)。conn 可注入(测试);无 conn 时自建连接
    (POSTGRES_URL)。返回清理的 thread 数;任何失败 fail-open 记日志
    (清理 job 崩掉不能拖垮服务)。
    """
    import logging
    import time as _time

    import psycopg

    now = _time.time()
    own = conn is None
    try:
        if own:
            from official_agent.config import get_settings

            url = dsn or get_settings().postgres_url
            url = url.replace("postgresql+psycopg://", "postgresql://")
            conn = psycopg.connect(url)
        assert conn is not None
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT thread_id FROM checkpoint_writes WHERE channel = %s",
            (_INTERRUPT_CHANNEL,),
        )
        thread_ids = [r[0] for r in cur.fetchall()]
        purged = 0
        for tid in thread_ids:
            # 挂起 vs 已恢复判别:最新事件仍是 __interrupt__ 写入
            # 才是「挂起未恢复」;恢复后闲置的 thread 不动(上下文不丢)
            cur.execute(
                "SELECT max(checkpoint_id) FROM checkpoint_writes "
                "WHERE thread_id = %s AND channel = %s",
                (tid, _INTERRUPT_CHANNEL),
            )
            row = cur.fetchone()
            int_cp = row[0] if row else None
            cur.execute(
                "SELECT max(checkpoint_id) FROM checkpoints WHERE thread_id = %s",
                (tid,),
            )
            row = cur.fetchone()
            last_cp = row[0] if row else None
            if not int_cp or not last_cp or str(int_cp) != str(last_cp):
                continue
            age = _uuid_timestamp_age_hours(str(last_cp), now)
            if age is None or age < max_age_hours:
                continue
            for table in _CHECKPOINT_TABLES:
                cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (tid,))
            purged += 1
        if own:
            conn.commit()
        if purged:
            logging.getLogger(__name__).info(
                "挂起载荷 TTL 清理:purge %d threads(>%dh 未恢复)", purged, max_age_hours
            )
        return purged
    except Exception:  # noqa: BLE001 — 清理 job fail-open
        logging.getLogger(__name__).warning("挂起载荷 TTL 清理失败(已忽略)", exc_info=True)
        return 0
    finally:
        if own and conn is not None:
            conn.close()
