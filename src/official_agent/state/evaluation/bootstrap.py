"""evaluation 表自举的「每进程一次」机制。

DDL 即使 no-op 也要在**整个事务期间**持表锁(实测 PG 17):
  CREATE INDEX IF NOT EXISTS   → ShareLock,挡住所有并发写
  ALTER TABLE ADD COLUMN IF NOT EXISTS → AccessExclusiveLock,连 SELECT 都挡
自举只允许发生在启动窗口(lifespan 或首次调用),绝不进数据路径——
把 ensure_* 放进每个数据函数就是把整张表的读写串行化(批量初筛时
worker 与管理面互相阻塞)。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_lock = threading.Lock()
_done: set[str] = set()


def ensure_once(key: str, build: Callable[[], None]) -> None:
    """按 key 幂等自举(双检锁;build 只在第一次真正执行)。"""
    if key in _done:
        return
    with _lock:
        if key in _done:
            return
        build()
        _done.add(key)


def mark_done(key: str) -> None:
    """显式置位(数据路径守卫用;启动入口 ensure_*_ready 每次都真跑 DDL)。"""
    _done.add(key)
