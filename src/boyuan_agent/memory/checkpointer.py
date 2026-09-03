"""MEM-01 checkpointer 工厂:三通道(A 对话/B 流水线/C Copilot)共用的状态底座。

- AsyncPostgresSaver(langgraph-checkpoint-postgres),独立 database
  `official_agent`(ADR-0007:存储只有 Postgres,与 Langfuse 同实例隔离)
- get_checkpointer() async 工厂;首用自动 setup()(建表,幂等)
- 池与事件循环绑定(anyio 要求):跨 loop 调用时自动重建,不跨 loop 复用
- 调用方负责 thread_id 规范(memory.threads.make_thread_id,SEC-07 v1)
"""

from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from boyuan_agent.config import get_settings

_SAVER: Any | None = None
_POOL: Any | None = None
_BOUND_LOOP: Any | None = None


def _database_url() -> str:
    return get_settings().postgres_url


async def get_checkpointer() -> AsyncPostgresSaver:
    """单例 AsyncPostgresSaver(按事件循环绑定);首用自动 setup()(幂等)。"""
    global _SAVER, _POOL, _BOUND_LOOP
    import asyncio

    loop = asyncio.get_running_loop()
    if _SAVER is not None and _BOUND_LOOP is loop:
        return _SAVER
    # loop 变了(新测试/新任务域):丢弃旧池,重建
    await aclose_checkpointer()
    _POOL = AsyncConnectionPool(
        conninfo=_database_url(),
        max_size=10,
        open=False,
        check=AsyncConnectionPool.check_connection,
    )
    _SAVER = AsyncPostgresSaver(_POOL)
    await _SAVER.setup()
    _BOUND_LOOP = loop
    return _SAVER


async def aclose_checkpointer() -> None:
    """进程退出/测试清理:释放连接池。"""
    global _SAVER, _POOL
    if _POOL is not None:
        await _POOL.close()
    _SAVER = None
    _POOL = None
