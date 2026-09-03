"""Postgres 连接池(MEM-01):threads/checkpointer 共用。

生命周期由调用方显式管理(入口组装时 open,退出时 close_pool)——池内
连接绑定事件循环,不能跨 loop 复用,因此不做模块级懒加载单例:
- 生产:入口(lifespan)调 await open_pool(),退出调 await close_pool()
- 测试:fixture 内 open/close,同 loop 完成
"""


from psycopg_pool import AsyncConnectionPool

from boyuan_agent.config import get_settings

_POOL: AsyncConnectionPool | None = None


def database_url() -> str:
    """连接串:ADR-0007 同实例,独立 database official_agent。"""
    return get_settings().postgres_url


async def open_pool() -> AsyncConnectionPool:
    """打开共享池(幂等:已开则直接返回)。"""
    global _POOL
    if _POOL is None or _POOL.closed:
        _POOL = AsyncConnectionPool(
            conninfo=database_url(),
            min_size=1,
            max_size=10,
            open=False,
            check=AsyncConnectionPool.check_connection,
        )
        await _POOL.open(wait=True, timeout=15)
    return _POOL


def get_pool() -> AsyncConnectionPool:
    """取已打开的共享池;未打开抛 RuntimeError(显式生命周期,不允许隐式开)。"""
    if _POOL is None or _POOL.closed:
        raise RuntimeError(
            "Postgres 池未打开:入口层需先 await open_pool()"
            "(开发期也可 uv run boyuan-agent startup 一次性打开)"
        )
    return _POOL


async def close_pool() -> None:
    global _POOL
    if _POOL is not None and not _POOL.closed:
        await _POOL.close()
    _POOL = None
