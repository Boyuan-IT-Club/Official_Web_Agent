"""Agent 配置热生效:配置指纹比对 + 变更后重建会话 agent。

PUT /admin/config 只失效 get_settings 缓存;活跃会话的 agent 是进程内
复用的,不重建就一直用旧 model/provider。每轮开始比对指纹,发现变化即用
新配置重建(身份/token 不变)。

指纹读取走线程池:get_effective_settings 每次直连 PG(agent_config 表,
无缓存),同步驱动不能占事件循环。重建用哪个构造器由调用方经 build_agent
注入,本模块不绑定具体装配实现。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from official_agent.config import HOT_KEYS, get_effective_settings
from official_agent.web.session_store import SessionState


def config_fingerprint() -> str:
    """当前生效配置(HOT_KEYS 值)的指纹;配置变更即变化。

    直连 PG(见模块 docstring),调用方须放线程池执行。
    """
    settings = get_effective_settings()
    return repr(tuple((k, getattr(settings, k, None)) for k in sorted(HOT_KEYS)))


async def ensure_fresh_agent(
    session: SessionState,
    *,
    checkpointer: Any,
    build_agent: Callable[..., Any],
) -> None:
    """比对会话记录的配置指纹,变了则用 build_agent 重建 session 的 agent。

    指纹读取失败(PG 不可用)保留原 agent 不重建——降级必须留痕,
    不静默:配置热生效悄悄失效只能靠这条日志发现。
    """
    try:
        current = await asyncio.to_thread(config_fingerprint)
    except Exception:  # noqa: BLE001 — PG 不可用 → 沿用原 agent(降级留痕)
        logging.getLogger(__name__).warning(
            "配置指纹读取失败,本轮沿用原 agent(热生效暂停)", exc_info=True
        )
        return
    if session.applied_config_fingerprint == current:
        return
    session.agent = build_agent(
        session.identity, user_token=session.user_token, checkpointer=checkpointer
    )
    session.applied_config_fingerprint = current
