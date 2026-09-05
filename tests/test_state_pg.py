"""MEM-01/#116 checkpointer 工厂单测:连接池契约 + setup 被 await + 资源清理。

关键回归:
- AsyncPostgresSaver.setup() 是 async 的,同步调用会静默不建表(真库 bug)。
- #116 联调踩坑:from_conn_string 是单连接,并发放大 + 断连毒化
  (「another command is already in progress」)→ 必须用 AsyncConnectionPool,
  且池连接必须 autocommit=True + prepare_threshold=0(langgraph 官方生产要求)。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from official_agent.state import pg


def _pool_mock() -> MagicMock:
    pool = MagicMock()
    pool.open = AsyncMock()
    pool.close = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_get_checkpointer_awaits_setup_and_yields_saver() -> None:
    pool = _pool_mock()
    fake_saver = MagicMock()
    fake_saver.setup = AsyncMock()

    with (
        patch.object(pg, "AsyncConnectionPool", return_value=pool),
        patch.object(pg, "AsyncPostgresSaver", return_value=fake_saver),
    ):
        async with pg.get_checkpointer() as saver:
            assert saver is fake_saver
            fake_saver.setup.assert_awaited_once()  # 防 sync 调用回归


@pytest.mark.asyncio
async def test_get_checkpointer_uses_configured_postgres_url() -> None:
    pool = _pool_mock()
    fake_saver = MagicMock()
    fake_saver.setup = AsyncMock()

    url = "postgresql://u:p@h:5432/official_agent"
    with (
        patch.object(pg, "AsyncConnectionPool", return_value=pool) as m_pool,
        patch.object(pg, "AsyncPostgresSaver", return_value=fake_saver),
        patch.object(pg, "get_settings") as m_settings,
    ):
        m_settings.return_value.postgres_url = url
        async with pg.get_checkpointer():
            pass
        assert m_pool.call_args.kwargs["conninfo"] == url  # 原样传递,无改写


@pytest.mark.asyncio
async def test_get_checkpointer_uses_pool_not_single_connection() -> None:
    """#116 回归钉:禁止退回 from_conn_string 单连接(断连毒化全服务)。"""
    pool = _pool_mock()
    fake_saver = MagicMock()
    fake_saver.setup = AsyncMock()

    with (
        patch.object(pg, "AsyncConnectionPool", return_value=pool) as m_pool,
        patch.object(pg, "AsyncPostgresSaver", return_value=fake_saver) as m_saver_cls,
    ):
        async with pg.get_checkpointer():
            pass

        kwargs = m_pool.call_args.kwargs
        assert kwargs["kwargs"]["autocommit"] is True
        assert kwargs["kwargs"]["prepare_threshold"] == 0
        assert kwargs["kwargs"]["row_factory"] is pg.dict_row
        assert kwargs["open"] is False  # 手动开池,wait/timeout 可控
        m_saver_cls.assert_called_once_with(pool)  # saver 背后是池


@pytest.mark.asyncio
async def test_get_checkpointer_closes_pool_on_exit() -> None:
    pool = _pool_mock()
    fake_saver = MagicMock()
    fake_saver.setup = AsyncMock()

    with (
        patch.object(pg, "AsyncConnectionPool", return_value=pool),
        patch.object(pg, "AsyncPostgresSaver", return_value=fake_saver),
    ):
        async with pg.get_checkpointer():
            pool.close.assert_not_called()  # 会话内不提前关池
        pool.close.assert_awaited_once()  # 退出必须关池
