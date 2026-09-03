"""thread 建档与软删除(MEM-01):SEC-07 五点契约的 v1 载体。

存储:Langfuse 同实例 PG 的独立 database `official_agent`(ADR-0007
「存储只有 Postgres」——SQLite/文件方案已被否,不引入第二存储)。

thread_id 规范 v1:{module}:{subject}:{random8}(8 位 hex 随机段,防猜/
防跨入口互踩)。SEC-07 细化(属主校验深化/并发/TTL)落地时改本模块。

建库职责:ensure 前若 official_agent 不存在则建(连 postgres 系统库执行)。
"""

import hashlib
import re
import secrets

import psycopg

from boyuan_agent.memory import db


def _ensure_database() -> None:
    """建 official_agent 库(幂等)。连 postgres 系统库执行。"""
    base = db.database_url().rsplit("/", 1)[0] + "/postgres"
    admin = psycopg.connect(base, autocommit=True)
    exists = admin.execute(
        "SELECT 1 FROM pg_database WHERE datname='official_agent'"
    ).fetchone()
    if not exists:
        admin.execute("CREATE DATABASE official_agent")
    admin.close()


async def _ensure_ddl(pool) -> None:  # noqa: ANN001 — pool 类型来自 psycopg_pool
    async with pool.connection() as conn:
        for ddl in (
            """CREATE TABLE IF NOT EXISTS agent_threads (
                thread_id     TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                channel       TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'active',
                created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                deleted_at    TIMESTAMPTZ
            )""",
            """CREATE INDEX IF NOT EXISTS idx_agent_threads_owner
               ON agent_threads(owner_user_id) WHERE status='active'""",
        ):
            await conn.execute(ddl)


async def get_pool_with_schema():
    """共享池 + 确保 official_agent 库/建档表就绪(幂等)。"""
    _ensure_database()
    pool = db.get_pool()
    await _ensure_ddl(pool)
    return pool


def make_thread_id(module: str, subject: str) -> str:
    """thread_id 规范 v1:{module}:{subject}:{random8}。

    subject 中的冒号会被换为下划线,保证三段结构可解析
    (SEC-07 恢复路径需要按段校验属主)。
    """
    subject = re.sub(r":+", "_", subject)
    return f"{module}:{subject}:{secrets.token_hex(4)}"


async def ensure_thread(thread_id: str, owner_user_id: int, channel: str) -> None:
    """建 thread 档案;幂等——已存在则不动(不改属主)。"""
    pool = await get_pool_with_schema()
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_threads (thread_id, owner_user_id, channel)"
            " VALUES (%s, %s, %s) ON CONFLICT (thread_id) DO NOTHING",
            (thread_id, owner_user_id, channel),
        )


async def soft_delete_thread(thread_id: str) -> None:
    """软删除:status→deleted,保留行(审计/回溯语义,SEC-06 拍板 TTL 前不物理删)。"""
    pool = await get_pool_with_schema()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_threads SET status='deleted', deleted_at=now()"
            " WHERE thread_id=%s",
            (thread_id,),
        )


async def get_thread_owner(thread_id: str) -> int | None:
    """属主查询(SEC-07 恢复路径):不存在或已软删除返回 None。"""
    pool = await get_pool_with_schema()
    async with pool.connection() as conn:
        row = await conn.execute(
            "SELECT owner_user_id FROM agent_threads"
            " WHERE thread_id=%s AND status='active'",
            (thread_id,),
        )
        fetched = await row.fetchone()
    return fetched[0] if fetched else None


def thread_fingerprint(thread_id: str) -> str:
    """thread 的短指纹(日志/审计展示用,不回原文)。"""
    return hashlib.sha256(thread_id.encode()).hexdigest()[:12]
