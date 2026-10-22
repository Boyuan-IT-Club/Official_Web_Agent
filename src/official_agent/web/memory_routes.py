"""长期记忆管理 API:/memories——用户查看与删除**本人**的记忆。

只有本人入口,没有按用户查别人记忆的管理端接口:记忆是从个人对话里整理的,
管理员也不该翻看。owner 一律取自当前登录身份,不接受参数指定。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from official_agent.graphs.identity import ResolvedIdentity
from official_agent.memory import store
from official_agent.memory.recall import memory_enabled_for
from official_agent.web.auth import authenticate as _authenticate

router = APIRouter()


def _owner(auth: tuple[ResolvedIdentity, str]) -> int:
    identity, _ = auth
    user_id = identity.get("user_id")
    if not user_id:
        raise HTTPException(status_code=403, detail="当前身份没有可管理的记忆")
    return int(user_id)


@router.get("/memories")
async def list_my_memories(
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> dict[str, Any]:
    """本人全部记忆;enabled 表示当前身份是否开启长期记忆(候选人不开启)。"""
    owner = _owner(auth)
    try:
        items = await asyncio.to_thread(store.list_memories, owner)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="记忆存储暂不可用,请稍后重试") from exc
    return {
        "enabled": memory_enabled_for(auth[0]),
        "items": [
            {
                "memory_id": m.memory_id,
                "content": m.content,
                "kind": m.kind,
                "updated_at": m.updated_at.isoformat() if m.updated_at else None,
            }
            for m in items
        ],
    }


@router.delete("/memories/{memory_id}")
async def delete_my_memory(
    memory_id: str,
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> dict[str, Any]:
    owner = _owner(auth)
    try:
        deleted = await asyncio.to_thread(store.delete_memory, owner, memory_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="记忆存储暂不可用,请稍后重试") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": memory_id}


@router.delete("/memories")
async def clear_my_memories(
    auth: Annotated[tuple[ResolvedIdentity, str], Depends(_authenticate)],
) -> dict[str, Any]:
    owner = _owner(auth)
    try:
        count = await asyncio.to_thread(store.delete_all, owner)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="记忆存储暂不可用,请稍后重试") from exc
    return {"deleted": count}
