"""真库集成测试的公共闸门:POSTGRES_URL 可连才跑,否则整档 skip。

数据层的语义(DDL 幂等、部分唯一索引、事务边界)mock 连接验不出来,
只能打真库;但真库不是每个开发者都有,所以是 skip 而非 fail。
CI 由 .github/workflows/ci.yml 的 postgres service 提供。
"""

from __future__ import annotations

import os
from typing import Any

import psycopg

PG_URL = os.environ.get("POSTGRES_URL", "")

# 锁等待上限:数据路径不该再取表级 DDL 锁,真取了就让它快速报错而不是挂死
PG_URL_WITH_LOCK_TIMEOUT = (
    f"{PG_URL}{'&' if '?' in PG_URL else '?'}options=-c%20lock_timeout%3D2000" if PG_URL else ""
)


def pg_available() -> bool:
    if not PG_URL:
        return False
    try:
        with psycopg.connect(PG_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — 连不上即 skip
        return False


def point_settings_at_pg(url: str | None = None) -> Any:
    """把全局 Settings 的 postgres_url 指向测试库,返回原值供还原。"""
    from official_agent.config import get_settings

    settings = get_settings()
    previous = settings.postgres_url
    settings.postgres_url = url or PG_URL
    return previous
