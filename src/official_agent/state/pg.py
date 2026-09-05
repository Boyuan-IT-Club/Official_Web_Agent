"""Postgres 状态设施(MEM-01):checkpointer 工厂。

- 与 Langfuse 共享同一实例(ADR-0007),库 official_agent,另建 database 隔离
- AsyncPostgresSaver:CLI/SSE 事件循环上用 async 连接,不阻塞线程池
- get_checkpointer() 上下文管理器:进入建连接池并建表(幂等),退出关池
- agent_threads 建档/软删除在 state/threads.py(SEC-07 属主载体)

注 1:必须用 AsyncConnectionPool 而非 from_conn_string——后者是单连接,
  全服务共享;任一请求在查询中途断连(用户关浏览器)即毒化连接
  (「another command is already in progress」),此后所有请求全挂
  (2026-09-05,M6 #116 联调实测踩坑)。连接池隔离并发与坏连接。
注 2:池连接必须 autocommit=True + prepare_threshold=0(langgraph 官方
  生产部署要求,checkpoint 写路径依赖);row_factory=dict_row 与
  from_conn_string 先例一致。
注 3:AsyncPostgresSaver 的 setup() 被覆写为 async,必须 await(同步调用
  会静默不建表)。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
