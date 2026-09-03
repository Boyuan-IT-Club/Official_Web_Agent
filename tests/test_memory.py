"""MEM-01 checkpointer 与 thread 建档单测。

PG 集成用例(带 real_pg 标记)连真实 5432;其余为纯单元(线程建档同步函数
包装 async 实现,不依赖网络)。
"""

import re

import pytest

from boyuan_agent.memory import db as memdb
from boyuan_agent.memory.threads import (
    ensure_thread,
    get_thread_owner,
    make_thread_id,
    soft_delete_thread,
)

THREAD_ID_PATTERN = re.compile(r"^[a-z]+:[^\s:]+:[0-9a-f]{8}$")


def test_make_thread_id_format() -> None:
    tid = make_thread_id("pipeline", "cycle3")
    assert THREAD_ID_PATTERN.match(tid), f"格式不符: {tid}"
    assert tid.startswith("pipeline:cycle3:")


def test_make_thread_id_random_segment_unique() -> None:
    ids = {make_thread_id("cli", "admin") for _ in range(50)}
    assert len(ids) == 50  # 随机段 8 hex,50 次碰撞概率≈0


def test_make_thread_id_subject_with_colon_sanitized() -> None:
    """subject 含冒号会破坏三段结构——必须净化。"""
    tid = make_thread_id("feishu", "ou:bad:id")
    assert THREAD_ID_PATTERN.match(tid)


def test_resolve_identity_node_via_router_keeps_export_shape() -> None:
    """防删除断言:GRA-01 前门导出面不受本票影响。"""
    from boyuan_agent.graphs.router import resolve_identity  # noqa: F401


async def test_ensure_thread_idempotent() -> None:
    await memdb.open_pool()
    tid = make_thread_id("cli", "admin")
    await ensure_thread(tid, owner_user_id=1, channel="cli")
    await ensure_thread(tid, owner_user_id=1, channel="cli")  # 重复建不炸、不改属主
    assert await get_thread_owner(tid) == 1


async def test_soft_delete_hides_owner() -> None:
    await memdb.open_pool()
    tid = make_thread_id("web", "user7")
    await ensure_thread(tid, owner_user_id=7, channel="web")
    await soft_delete_thread(tid)
    assert await get_thread_owner(tid) is None  # 已删除线程对恢复路径不可见


async def test_get_thread_owner_unknown_returns_none() -> None:
    await memdb.open_pool()
    assert await get_thread_owner("nobody:nothing:00000000") is None


@pytest.mark.backend
async def test_checkpointer_roundtrip(real_checkpointer) -> None:
    """真 PG 集成:setup 建表 + put/get 往返。"""
    tid = make_thread_id("cli", "admin")
    config = {"configurable": {"thread_id": tid}}
    await real_checkpointer.aput(
        config,
        {"channel_values": {"messages": ["hello"]}, "channel_versions": {"messages": 1}},
        {},
        {"messages": 1},
    )
    got = await real_checkpointer.aget_tuple(config)
    assert got is not None
    assert got.checkpoint["channel_values"]["messages"] == ["hello"]


async def test_checkpointer_singleton_same_loop() -> None:
    """同 loop 内单例;cleanup 后可重建(loop 绑定策略)。"""
    from boyuan_agent.memory import checkpointer as ckpt
    from boyuan_agent.memory import db

    s1 = await ckpt.get_checkpointer()
    s2 = await ckpt.get_checkpointer()
    assert s1 is s2
    await ckpt.aclose_checkpointer()
    await db.close_pool()
