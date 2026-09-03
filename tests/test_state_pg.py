"""MEM-01 checkpointer 工厂单测:setup 被 await、URL 传递、mock 连接下可用。

关键回归:AsyncPostgresSaver.setup() 是 async 的,同步调用会静默不建表
(真库 bug:第一轮跑通但表不存在)。此测试断言 setup 被 await。
"""

from unittest.mock import AsyncMock, patch

import pytest

from boyuan_agent.state import pg


@pytest.mark.asyncio
async def test_get_checkpointer_awaits_setup_and_yields_saver() -> None:
    fake_saver = AsyncMock()
    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_saver
    fake_cm.__aexit__ = AsyncMock(return_value=False)

    with patch.object(
        pg.AsyncPostgresSaver, "from_conn_string", return_value=fake_cm
    ) as m_from:
        async with pg.get_checkpointer() as saver:
            assert saver is fake_saver
            fake_saver.setup.assert_awaited_once()  # 防 sync 调用回归
        m_from.assert_called_once()


@pytest.mark.asyncio
async def test_get_checkpointer_uses_configured_postgres_url() -> None:
    fake_saver = AsyncMock()
    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_saver
    fake_cm.__aexit__ = AsyncMock(return_value=False)

    url = "postgresql://u:p@h:5432/official_agent"
    with (
        patch.object(pg.AsyncPostgresSaver, "from_conn_string", return_value=fake_cm) as m_from,
        patch.object(pg, "get_settings") as m_settings,
    ):
        m_settings.return_value.postgres_url = url
        async with pg.get_checkpointer():
            pass
        called_url = m_from.call_args.args[0]
        assert called_url == url  # 原样传递,无 postgresql+asyncpg 改写


@pytest.mark.asyncio
async def test_get_checkpointer_close_connection_on_exit() -> None:
    fake_saver = AsyncMock()
    fake_cm = AsyncMock()
    fake_cm.__aenter__.return_value = fake_saver
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    with patch.object(pg.AsyncPostgresSaver, "from_conn_string", return_value=fake_cm):
        async with pg.get_checkpointer():
            pass
        fake_cm.__aexit__.assert_awaited_once()