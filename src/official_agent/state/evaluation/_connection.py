"""evaluation 状态面的 PG 连接:进程级 psycopg_pool 连接池。

此前每次 `_conn()` 直连(psycopg.connect):批量初筛时 worker 与管理面
各自反复握手。池化后连接复用;`with _conn() as conn` 退出语义与直连一致
(干净退出提交,异常回滚),对调用方透明。

测试接缝:单测 patch 本模块 `_conn` 替换单连接来源,池只在真实路径建立;
集成测试改 `postgres_url` 后调 `reset_pool()` 重建,保证新 URL 生效。
"""

from __future__ import annotations

import threading
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from official_agent.config import get_settings

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> ConnectionPool:
    """进程级连接池(懒建)。min_size=1:低频管理面不预热,批量时复用。"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    get_settings().postgres_url,
                    min_size=1,
                    max_size=8,
                    open=True,
                    kwargs={"row_factory": dict_row},
                )
    return _pool


def reset_pool() -> None:
    """关闭并清空池(测试改 postgres_url 后重建用;生产不调用)。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def _conn() -> psycopg.Connection[dict[str, Any]]:
    """签出一条池化连接(with 退出自动归还;干净退出提交,异常回滚)。"""
    return get_pool().connection()
