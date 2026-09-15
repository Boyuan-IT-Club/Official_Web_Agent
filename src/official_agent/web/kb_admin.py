"""KB 管理 API(RAG #134 R2):/api/agent/admin/kb* —— 知识条目维护面。

- 权限:独立权限码 ``kb:manage``(#121:与 agent:monitor 分离,可单独授权
  某管理员管知识库;Backend V41 起种子落地,R5 代理转发同码校验)
- 创建/更新即入库重嵌(分块+embedding 单事务);「重嵌」端点按已存内容
  重建向量——换 embedding 模型后逐条补齐用
- store 层同步 psycopg,路由内一律 asyncio.to_thread,不阻塞事件循环
"""

import asyncio
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from official_agent.graphs.identity import ResolvedIdentity
from official_agent.kb import store as kb_store
from official_agent.kb.embedding import EmbeddingError, EmbeddingNotConfiguredError
from official_agent.kb.schema import KbSchemaError
from official_agent.kb.store import KbValidationError
from official_agent.web.routes import _authenticate

router = APIRouter()


async def _require_kb_manage(
    request: Request, authorization: Annotated[str | None, Header()] = None
):
    """KB 管理 API 认证:官网 JWT → resolve → permission_codes 含 kb:manage。"""
    identity, _ = await _authenticate(request, authorization)
    codes = identity.get("permission_codes") or []
    if "kb:manage" not in codes:
        raise HTTPException(status_code=403, detail="需要 kb:manage 权限")
    return identity


class KbSourceUpsert(BaseModel):
    """创建/更新知识条目。type=faq 必须 question/answer;type=doc 必须 content_md。"""

    title: str = Field(min_length=1)
    type: Literal["faq", "doc"]
    kind: Literal["normal", "test"] = "normal"
    tags: list[str] = Field(default_factory=list)
    question: str = ""
    answer: str = ""
    content_md: str = ""


class KbEnabledBody(BaseModel):
    enabled: bool


def _actor(identity: ResolvedIdentity) -> str:
    """操作人落库标识(列表回显 updated_by)。"""
    return f"user:{identity.get('user_id', '?')}"


def _to_input(body: KbSourceUpsert, identity: ResolvedIdentity) -> kb_store.SourceInput:
    return kb_store.SourceInput(
        title=body.title,
        type=body.type,
        kind=body.kind,
        tags=tuple(body.tags),
        question=body.question,
        answer=body.answer,
        content_md=body.content_md,
        updated_by=_actor(identity),
    )


def _map_store_error(exc: Exception) -> HTTPException:
    """store/embedding 异常 → HTTP 语义(测试/前端只见状态码+detail)。"""
    if isinstance(exc, KbValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, EmbeddingNotConfiguredError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, EmbeddingError):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, KbSchemaError):
        return HTTPException(status_code=500, detail=str(exc))
    return HTTPException(status_code=500, detail=f"KB 操作失败:{exc}")


@router.get("/admin/kb/sources")
async def list_kb_sources(
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    page: int = 1,
    size: int = 20,
    kind: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """分页列表(source_id/标题/kind/标签/启停/更新人/块数)。"""
    try:
        result = await asyncio.to_thread(
            kb_store.list_sources, page=page, size=size, kind=kind, keyword=keyword
        )
    except Exception as exc:  # noqa: BLE001 — 统一映射
        raise _map_store_error(exc) from exc
    result["items"] = [asdict(r) for r in result["items"]]
    return result


@router.post("/admin/kb/sources", status_code=201)
async def create_kb_source(
    body: KbSourceUpsert,
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """新建并入库(分块+embedding)。EMBED_* 未配置 → 503(检查点①)。"""
    try:
        source_id = await kb_store.ingest_source(_to_input(body, identity))
    except Exception as exc:  # noqa: BLE001
        raise _map_store_error(exc) from exc
    return {"source_id": source_id}


@router.get("/admin/kb/sources/{source_id}")
async def get_kb_source(
    source_id: str,
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """详情(含 faq/doc 内容与块数)。"""
    record = await asyncio.to_thread(kb_store.get_source, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return asdict(record)


@router.put("/admin/kb/sources/{source_id}")
async def update_kb_source(
    source_id: str,
    body: KbSourceUpsert,
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """更新并重嵌(条目级增量;内容表整体替换,faq↔doc 迁移安全)。"""
    exists = await asyncio.to_thread(kb_store.get_source, source_id)
    if not exists:
        raise HTTPException(status_code=404, detail="条目不存在")
    try:
        await kb_store.ingest_source(_to_input(body, identity), source_id=source_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_store_error(exc) from exc
    return {"source_id": source_id}


@router.put("/admin/kb/sources/{source_id}/enabled")
async def set_kb_source_enabled(
    source_id: str,
    body: KbEnabledBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """启/停用。停用立即退出生检索(chunks 保留,可复活)。"""
    try:
        changed = await asyncio.to_thread(
            kb_store.set_source_enabled,
            source_id,
            body.enabled,
            updated_by=_actor(identity),
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_store_error(exc) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"source_id": source_id, "enabled": body.enabled}


@router.delete("/admin/kb/sources/{source_id}")
async def delete_kb_source(
    source_id: str,
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """删除(内容表/chunks 级联)。"""
    try:
        deleted = await asyncio.to_thread(kb_store.delete_source, source_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_store_error(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"deleted": True, "source_id": source_id}


@router.post("/admin/kb/sources/{source_id}/reembed")
async def reembed_kb_source(
    source_id: str,
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """按已存内容重建向量(重跑分块+embedding;换模型后逐条补齐)。"""
    record = await asyncio.to_thread(kb_store.get_source, source_id)
    if record is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    source = kb_store.SourceInput(
        title=record.title,
        type=record.type,
        kind=record.kind,
        tags=tuple(record.tags),
        question=record.question,
        answer=record.answer,
        content_md=record.content_md,
        updated_by=_actor(identity),
    )
    try:
        await kb_store.ingest_source(source, source_id=source_id)
    except Exception as exc:  # noqa: BLE001
        raise _map_store_error(exc) from exc
    return {"source_id": source_id, "reembedded": True}
