"""通用的进程级连接池:按 postgres_url 懒建,签出即 with。

新模块直接用这里;评测线(state/evaluation/_connection)与知识库(kb/store)
各有一份历史实现,行为与此相同。按 URL 分池:集成测试会把 URL 指到临时库,
共用一个池会连错库。
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from official_agent.config import get_settings

_pools: dict[str, ConnectionPool] = {}
_lock = threading.Lock()


def pooled_conn() -> AbstractContextManager[psycopg.Connection[dict[str, Any]]]:
    """签出一条池化连接——返回上下文管理器,必须 with;干净退出提交,异常回滚。"""
    url = get_settings().postgres_url
    pool = _pools.get(url)
    if pool is None:
        with _lock:
            pool = _pools.get(url)
            if pool is None:
                pool = ConnectionPool(
                    url, min_size=1, max_size=4, open=True, kwargs={"row_factory": dict_row}
                )
                _pools[url] = pool
    # ignore:池的泛型推断不到 kwargs 里的 dict_row,实际签出的就是 dict_row 连接
    return pool.connection()  # type: ignore[return-value]


def reset_pools() -> None:
    """关闭全部连接池(测试收尾用;生产不调用)。"""
    with _lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
