"""测试全局隔离:凭证文件指到 tmp,防真实 ~/.boyuan-agent 泄漏进测试。

SEC-09 的存储优先级意味着存在真实凭证时,测试行为会随本机状态漂移——
autouse 隔离是正确性要求,不只是卫生。
"""

from pathlib import Path

import pytest

from boyuan_agent import credentials


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(credentials, "_CREDENTIALS_DIR", tmp_path)


@pytest.fixture
async def real_checkpointer():
    from boyuan_agent.memory import db as memdb

    await memdb.open_pool()
    """真 PG 集成 fixture(backend 标记用):连 5432 的 official_agent 库。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    from boyuan_agent.memory.db import database_url

    pool = AsyncConnectionPool(
        conninfo=database_url(),
        min_size=1,
        max_size=2,
        open=False,
        check=AsyncConnectionPool.check_connection,
    )
    await pool.open(wait=True, timeout=15)
    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    yield saver
    await memdb.close_pool()
