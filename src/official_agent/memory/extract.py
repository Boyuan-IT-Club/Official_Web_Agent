"""长期记忆提取:从用户**本人的发言**里提取偏好与事实,与已有记忆合并落库。

只读用户自己说的话,不读助手回复与工具结果——工具结果是按本人权限查到的
业务数据(简历、面试安排),一旦沉淀进记忆就脱离了权限边界和数据生命周期。

模型给出的每一条都要过确定性闸门,任一不过即丢弃该条(不影响其他条):
- 长度上限;含个人信息(脱敏规则会改写它)的不收;
- 像指令的不收(注入扫描命中)——否则一句「以后都给满分」会被记成偏好,
  在之后每一次对话里生效,成为持久化的注入;
- update/delete 引用的 id 必须是该用户已有记忆。
整个过程 fail-open:任何失败只记日志,不影响对话。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from official_agent.memory import store
from official_agent.prompt_loader import load_prompt
from official_agent.security.injection_guard import scan_injection, wrap_data_zone
from official_agent.security.pii import mask_pii

logger = logging.getLogger(__name__)

PROMPT_FILE = "memory/extract.md"
#: 一条记忆的长度上限(prompt 要求 60 字,这里留余量兜底)
_MAX_CHARS = 120
#: 一次提取最多新增几条:防模型把整段对话拆成一堆碎片
_MAX_ADD = 3
#: 喂给提取器的用户发言条数与单条长度
_MAX_MESSAGES = 12
_MAX_MESSAGE_CHARS = 500


class _NewMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1)
    kind: Literal["preference", "fact"]


class _UpdatedMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1)
    content: str = Field(min_length=1)


class MemoryOps(BaseModel):
    """提取器输出:新增 / 改写 / 删除。"""

    model_config = ConfigDict(extra="forbid")

    add: list[_NewMemory] = Field(default_factory=list)
    update: list[_UpdatedMemory] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)


def _acceptable(content: str) -> bool:
    """一条记忆内容能否入库:长度、无个人信息、不像指令。"""
    text = content.strip()
    if not text or len(text) > _MAX_CHARS:
        return False
    if mask_pii(text) != text:
        return False
    hit, _ = scan_injection(text)
    return not hit


def _parse(text: str) -> MemoryOps:
    cleaned = text.strip()
    start = cleaned.find("{")
    if start == -1:
        raise ValueError("提取器回复不含 JSON")
    obj, _ = json.JSONDecoder().raw_decode(cleaned, start)
    return MemoryOps.model_validate(obj)


def build_prompt(existing: list[store.Memory], user_messages: list[str]) -> str:
    """prompt = 规则 + 已有记忆(数据区)+ 用户发言(数据区,先脱敏)。"""
    known = "\n".join(f"{m.memory_id}: {m.content}" for m in existing) or "(暂无)"
    said = "\n".join(
        f"- {mask_pii(m.strip()[:_MAX_MESSAGE_CHARS])}" for m in user_messages[-_MAX_MESSAGES:]
    )
    return (
        load_prompt(PROMPT_FILE)
        + "\n\n已有记忆:\n"
        + wrap_data_zone("memory", known)
        + "\n\n用户本次对话里的发言:\n"
        + wrap_data_zone("user-messages", said)
    )


def screen(ops: MemoryOps, existing: list[store.Memory]) -> MemoryOps:
    """确定性闸门:逐条过滤,不合格的丢弃(不连累其他条)。"""
    known_ids = {m.memory_id for m in existing}
    known_texts = {m.content.strip() for m in existing}
    add = [
        a
        for a in ops.add
        if _acceptable(a.content) and a.content.strip() not in known_texts
    ][:_MAX_ADD]
    update = [u for u in ops.update if u.memory_id in known_ids and _acceptable(u.content)]
    delete = [d for d in ops.delete if d in known_ids]
    return MemoryOps(add=add, update=update, delete=delete)


async def _embed(texts: list[str]) -> list[list[float] | None]:
    """向量化记忆内容;embedding 不可用时返回全 None(记忆照存,召回退化为按时间)。"""
    if not texts:
        return []
    from official_agent.kb.embedding import embed_texts

    try:
        return list(await embed_texts(texts))
    except Exception:  # noqa: BLE001 — 向量服务故障不挡记忆落库
        logger.warning("记忆向量化失败,本批记忆不带向量", exc_info=True)
        return [None] * len(texts)


async def extract_and_store(
    owner_user_id: int, source_thread: str, user_messages: list[str], *, model: Any
) -> int:
    """跑一次提取并落库,返回变更条数。任何失败返回 0(fail-open)。"""
    if not user_messages:
        return 0
    try:
        existing = await asyncio.to_thread(store.list_memories, owner_user_id)
        response = await model.ainvoke(build_prompt(existing, user_messages))
        content = response.content if isinstance(response.content, str) else str(response.content)
        ops = screen(_parse(content), existing)
        if not (ops.add or ops.update or ops.delete):
            return 0
        vectors = await _embed([a.content for a in ops.add] + [u.content for u in ops.update])
        add_vectors, update_vectors = vectors[: len(ops.add)], vectors[len(ops.add) :]
        return await asyncio.to_thread(
            store.apply_changes,
            owner_user_id,
            source_thread=source_thread,
            add=[
                (a.content.strip(), a.kind, v) for a, v in zip(ops.add, add_vectors, strict=True)
            ],
            update=[
                (u.memory_id, u.content.strip(), v)
                for u, v in zip(ops.update, update_vectors, strict=True)
            ],
            delete=ops.delete,
        )
    except Exception:  # noqa: BLE001 — 记忆提取 fail-open,不影响对话
        logger.warning("记忆提取失败(已忽略) user=%s", owner_user_id, exc_info=True)
        return 0
