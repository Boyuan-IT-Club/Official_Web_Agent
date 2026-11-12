"""组织记忆管理 API:/admin/org-memory*——勾选、退役、续期、列表。

组织记忆是往届复盘里经人确认过的经验,召回进管理员与社员的对话。它只由这里的
人工动作写入:
- 权限:全部要 evaluation:run(与发布报告同级;后端目前没有更高一级的权限码)
- 勾选、退役、续期都是人工决策:先验对象(不存在或状态不对 → 此时零副作用),
  再写审计(写不进去 → 503,未执行),最后改状态
- 只写 agent 自己的表,不经确认令牌——点按钮的那个人就是确认

接口不受 org_memory_enabled 开关影响:开关管的是召回进对话,整理记忆可以先做。
"""

import asyncio
import logging
import secrets
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from official_agent.config import get_settings
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.memory import org_store
from official_agent.retrospective import lessons
from official_agent.state import audit
from official_agent.state.retrospective import report_store
from official_agent.web.auth import require_any as _require_any

logger = logging.getLogger(__name__)

router = APIRouter()

_require_curator = _require_any("evaluation:run")


class ApproveBody(BaseModel):
    """勾选一条候选教训。content 不给就用报告里的原文;给了就是管理员改写后的版本。"""

    cycle_id: int = Field(ge=1)
    version: int = Field(ge=1)
    lesson_index: int = Field(ge=0)
    content: str | None = None
    audience: Literal["admin", "member"] = "admin"
    valid_days: int | None = Field(default=None, ge=1)


class RetireBody(BaseModel):
    memory_id: str = Field(min_length=1)


class RenewBody(BaseModel):
    memory_id: str = Field(min_length=1)
    valid_days: int | None = Field(default=None, ge=1)


def _user_id(identity: ResolvedIdentity) -> int:
    return int(identity.get("user_id") or 0)


def _view(memory: org_store.OrgMemory) -> dict[str, Any]:
    body = asdict(memory)
    body["metric_ids"] = list(body.pop("evidence").get("metric_ids") or [])
    return body


async def _audit(user_id: int, action: dict[str, Any], decision: str, result: str) -> None:
    """写一条审计;写不进去就 503,调用方据此不执行后面的动作(ADR-0006)。"""
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"org-memory:{secrets.token_hex(4)}",
            acting_user_id=user_id,
            channel="retrospective",
            agent="org-memory-admin",
            action=action,
            decision=f"u{user_id}:{decision}",
            result=result,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="审计服务不可用,操作未执行,请稍后重试") from exc


async def _existing(memory_id: str) -> org_store.OrgMemory:
    """要退役或续期的那条;不存在 404,已退役 409。"""
    try:
        memory = await asyncio.to_thread(org_store.get, memory_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("组织记忆读取失败 memory_id=%s", memory_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询组织记忆失败,请稍后重试") from exc
    if memory is None:
        raise HTTPException(status_code=404, detail="组织记忆不存在")
    if memory.status != org_store.STATUS_APPROVED:
        raise HTTPException(status_code=409, detail="这条组织记忆已经退役")
    return memory


@router.get("/admin/org-memory")
async def list_org_memories(
    _: Annotated[ResolvedIdentity, Depends(_require_curator)],
    status: Annotated[Literal["approved", "retired"] | None, Query()] = None,
) -> dict[str, Any]:
    """全部组织记忆(最近勾选的在前);expired 为真的是过了保质期、等人续期或退役的。

    enabled 表示组织记忆现在会不会被召回进对话。
    """
    try:
        items = await asyncio.to_thread(org_store.list_memories, status=status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("组织记忆列表查询失败", exc_info=True)
        raise HTTPException(status_code=500, detail="查询组织记忆失败,请稍后重试") from exc
    return {
        "enabled": bool(get_settings().org_memory_enabled),
        "items": [_view(m) for m in items],
        "total": len(items),
    }


@router.post("/admin/org-memory/approve", status_code=201)
async def approve_org_memory(
    body: ApproveBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_curator)],
) -> dict[str, Any]:
    """勾选已发布报告里的一条候选教训,存成组织记忆。"""
    user_id = _user_id(identity)
    try:
        report = await asyncio.to_thread(
            report_store.get_report, body.cycle_id, version=body.version
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘报告读取失败 cycle_id=%s", body.cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询报告失败,请稍后重试") from exc
    if report is None:
        raise HTTPException(status_code=404, detail="指定版本的报告不存在")
    try:
        lesson = lessons.lesson_of(report, body.lesson_index)
        content = lessons.check_content(body.content or lesson["content"])
    except lessons.ReportNotPublishedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except lessons.LessonNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except lessons.ContentRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    valid_days = lessons.resolve_valid_days(body.valid_days)

    await _audit(
        user_id,
        {
            "op": "approve_org_memory",
            "cycle_id": body.cycle_id,
            "version": body.version,
            "lesson_index": body.lesson_index,
            "audience": body.audience,
            "valid_days": valid_days,
            "rewritten": content != lesson["content"],
        },
        "approve",
        "勾选复盘教训为组织记忆",
    )
    try:
        memory = await lessons.approve_lesson(
            report,
            body.lesson_index,
            content=content,
            audience=body.audience,
            valid_days=valid_days,
            approved_by=user_id,
        )
    except org_store.AlreadyApprovedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("组织记忆勾选失败 cycle_id=%s", body.cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="保存组织记忆失败,请稍后重试") from exc
    return _view(memory)


@router.post("/admin/org-memory/retire")
async def retire_org_memory(
    body: RetireBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_curator)],
) -> dict[str, Any]:
    """退役一条组织记忆:不再召回,记录保留。"""
    user_id = _user_id(identity)
    await _existing(body.memory_id)
    await _audit(
        user_id,
        {"op": "retire_org_memory", "memory_id": body.memory_id},
        "retire",
        "退役组织记忆",
    )
    try:
        retired = await asyncio.to_thread(org_store.retire, body.memory_id, retired_by=user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("组织记忆退役失败 memory_id=%s", body.memory_id, exc_info=True)
        raise HTTPException(status_code=500, detail="退役组织记忆失败,请稍后重试") from exc
    if not retired:
        # 验对象之后、执行之前被别人退役了
        raise HTTPException(status_code=409, detail="这条组织记忆已经退役")
    return {"memory_id": body.memory_id, "status": org_store.STATUS_RETIRED}


@router.post("/admin/org-memory/renew")
async def renew_org_memory(
    body: RenewBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_curator)],
) -> dict[str, Any]:
    """续期:确认这条经验今年还成立,保质期改为从现在起 valid_days 天。"""
    user_id = _user_id(identity)
    await _existing(body.memory_id)
    valid_days = lessons.resolve_valid_days(body.valid_days)
    await _audit(
        user_id,
        {"op": "renew_org_memory", "memory_id": body.memory_id, "valid_days": valid_days},
        "renew",
        "续期组织记忆",
    )
    try:
        memory = await asyncio.to_thread(org_store.renew, body.memory_id, valid_days=valid_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("组织记忆续期失败 memory_id=%s", body.memory_id, exc_info=True)
        raise HTTPException(status_code=500, detail="续期组织记忆失败,请稍后重试") from exc
    if memory is None:
        raise HTTPException(status_code=409, detail="这条组织记忆已经退役")
    return _view(memory)
