"""把管理员批准的草稿写进知识库。

这是整个运营闭环里唯一向知识库写内容的地方,而且只由管理面的批准接口调用——
离线流水线跑完时什么都没有发布。发布是幂等的:新 FAQ 落到由草稿 id 决定的
source_id 上,补问法在目标 FAQ 已有同样问法时什么都不做,所以批准中途失败后重试
不会发布两份。
"""

from __future__ import annotations

from typing import Any

from official_agent.kb import store as kb_store
from official_agent.kb.embedding import Embedder
from official_agent.kb_ops.draft import KIND_ALT_QUESTION


class DraftNotPublishableError(ValueError):
    """草稿的内容还不能发布(管理面映射为 400);消息是面向管理员的受控文案。"""


def published_source_id(draft_id: int) -> str:
    """新 FAQ 草稿发布后落到的条目 id。由草稿 id 决定,重复发布覆盖同一条。"""
    return f"kb_draft{draft_id}"


def check_publishable(draft: dict[str, Any]) -> None:
    """草稿内容是否齐备;不齐抛 DraftNotPublishableError。不检查审核状态。"""
    if not str(draft["question"]).strip():
        raise DraftNotPublishableError("草稿的问题不能为空")
    if draft["kind"] == KIND_ALT_QUESTION:
        if not draft.get("target_source_id"):
            raise DraftNotPublishableError("补问法草稿缺少目标条目")
        return
    if not str(draft["answer"]).strip():
        raise DraftNotPublishableError("草稿还没有答案:请先补上答案再批准")


async def publish_draft(
    draft: dict[str, Any], *, actor: str, embedder: Embedder | None = None
) -> str:
    """把草稿写进知识库,返回落到的 source_id。调用前须先过 check_publishable。

    补问法:给目标 FAQ 加一种问法并重建它的块。其余两种:新建一条 FAQ,标题用主题名,
    绑定草稿上的招新周期。actor 记为条目的更新人。目标条目不存在等内容问题抛
    KbValidationError,嵌入端点问题抛 EmbeddingError;两种情况下知识库都没有被改动。
    """
    if draft["kind"] == KIND_ALT_QUESTION:
        target = str(draft["target_source_id"])
        await kb_store.add_alt_question(
            target, str(draft["question"]), updated_by=actor, embedder=embedder
        )
        return target
    source_id = published_source_id(int(draft["draft_id"]))
    await kb_store.ingest_source(
        kb_store.SourceInput(
            title=str(draft["topic"]),
            type="faq",
            question=str(draft["question"]),
            answer=str(draft["answer"]),
            updated_by=actor,
            cycle_id=draft.get("cycle_id"),
        ),
        embedder=embedder,
        source_id=source_id,
    )
    return source_id
