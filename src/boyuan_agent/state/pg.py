"""Postgres 状态设施(MEM-01):checkpointer 工厂。

- 与 Langfuse 共享同一实例(ADR-0007),库 official_agent,另建 database 隔离
- AsyncPostgresSaver:CLI/SSE 事件循环上用 async 连接,不阻塞线程池
- get_checkpointer() 上下文管理器:进入建立连接并建表(幂等),退出关闭
- agent_threads 建档/软删除在 state/threads.py(SEC-07 属主载体)

注:AsyncPostgresSaver.from_conn_string 是异步上下文管理器,__aenter__ 不建表;
  setup() 在 AsyncPostgresSaver 中被覆写为 async,必须 await(同步调用会静默不建表)。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from boyuan_agent.config import get_settings


@asynccontextmanager
async def get_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """Yields 共享 AsyncPostgresSaver,进入时建立连接并建 SDK 表(幂等)。

    用法:
        async with get_checkpointer() as saver:
            agent = create_agent(..., checkpointer=saver)
    """
    async with AsyncPostgresSaver.from_conn_string(get_settings().postgres_url) as saver:
        await saver.setup()  # async setup(AsyncPostgresSaver 覆写):建表幂等
        yield saver