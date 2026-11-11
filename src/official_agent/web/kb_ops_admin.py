"""知识库运营管理 API:/api/agent/admin/kb/ops* —— 触发离线分析、查看主题清单。

- 权限:``kb:manage``,与知识条目维护同一个权限码——能改知识库的人才需要看它缺什么
- 一个触发入口加两个只读接口;这里的任何接口都不向知识库写内容
- 路由由 kb_admin 的 router 一并注册,不单独挂到 app
- 主题的样本提问按检索日志行 id 回查:所在会话已删除的样本不再出现
"""

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from official_agent.graphs.identity import ResolvedIdentity
from official_agent.kb import query_log
from official_agent.kb_ops import runner as kb_ops_runner
from official_agent.kb_ops.cluster import gap_hint
from official_agent.kb_ops.triage import looks_like_instruction
from official_agent.security.pii import mask_pii
from official_agent.state import audit
from official_agent.state import kb_ops as kb_ops_state
from official_agent.web.auth import require_any as _require_any

logger = logging.getLogger(__name__)

router = APIRouter()

_require_kb_manage = _require_any("kb:manage")

#: 每个主题最多展示几条样本提问。库里多存了几条备用:样本所在会话被删后还有得补
_SAMPLES_SHOWN = 3
#: scope 查询参数的取值。actionable = 需要人处理的(该由知识库回答的、该由工具回答
#: 的、还没归类的),不含模型判为无关或闲聊的
_SCOPE_ACTIONABLE = "actionable"
_SCOPE_ALL = "all"
_SCOPE_UNTRIAGED = "untriaged"
_TRIAGED_SCOPES = ("official_knowledge", "personal_data", "out_of_scope", "chitchat")
_ACTIONABLE_SCOPES = ("official_knowledge", "personal_data", None)


class RunBody(BaseModel):
    """触发一次分析。days 是往回看多少天的检索日志。"""

    days: int = Field(default=7, ge=1, le=30)


@router.post("/admin/kb/ops/runs", status_code=202)
async def start_kb_ops_run(
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    body: RunBody | None = None,
) -> dict[str, Any]:
    """启动一次离线分析(后台执行),返回 run_id。已有分析在跑 → 409。"""
    days = body.days if body else RunBody().days
    user_id = int(identity.get("user_id") or 0)
    try:
        run_id = await kb_ops_runner.start_run(days=days, triggered_by=user_id)
    except kb_ops_runner.RunInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 原始异常可能带连接串,只进日志
        logger.warning("kb_ops 启动失败", exc_info=True)
        raise HTTPException(status_code=500, detail="启动分析失败,请稍后重试") from exc
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"kbops:{run_id}",
            acting_user_id=user_id,
            channel="kb_ops",
            agent="kb-ops-runner",
            action={"op": "run_kb_ops_mining", "run_id": run_id, "days": days},
            decision=f"u{user_id}:run",
            result="已启动知识库运营分析",
        )
    except Exception:  # noqa: BLE001
        # 分析已经在跑,而且它不发布任何内容:审计缺一条「谁触发的」不该让请求失败,
        # 但必须在日志里看得见(触发人另记在运行行的 triggered_by 上)
        logger.warning("kb_ops 触发审计写入失败 run_id=%s", run_id, exc_info=True)
    return {"run_id": run_id, "status": kb_ops_state.RUN_RUNNING}


@router.get("/admin/kb/ops/runs")
async def list_kb_ops_runs(
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    limit: int = 20,
) -> dict[str, Any]:
    """最近的分析运行,新的在前(状态、时间窗、统计量、失败原因)。"""
    limit = max(1, min(limit, 50))
    try:
        runs = await asyncio.to_thread(kb_ops_state.list_runs, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_ops 运行列表查询失败", exc_info=True)
        raise HTTPException(status_code=500, detail="查询失败,请稍后重试") from exc
    return {"items": runs, "total": len(runs)}


@router.get("/admin/kb/ops/topics")
async def list_kb_ops_topics(
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    run_id: str | None = None,
    scope: str = _SCOPE_ACTIONABLE,
) -> dict[str, Any]:
    """一次运行的主题清单,按影响面从大到小。

    run_id 缺省取最近一次成功的运行;一次都没成功过时返回空清单(run 为 null)。
    scope:actionable(缺省)/ all / untriaged / 四种归类之一。
    """
    if scope not in (_SCOPE_ACTIONABLE, _SCOPE_ALL, _SCOPE_UNTRIAGED, *_TRIAGED_SCOPES):
        raise HTTPException(status_code=400, detail="scope 取值无效")
    try:
        if run_id is None:
            run = await asyncio.to_thread(kb_ops_state.latest_succeeded_run)
        else:
            run = await asyncio.to_thread(kb_ops_state.get_run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="运行不存在")
        if run is None:
            return {"run": None, "items": [], "total": 0}
        topics = await asyncio.to_thread(kb_ops_state.list_topics, run["run_id"])
        topics = [t for t in topics if _in_scope(t.get("scope"), scope)]
        sample_ids = [qid for t in topics for qid in t["sample_query_ids"]]
        queries = await asyncio.to_thread(query_log.get_queries, sample_ids)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_ops 主题清单查询失败", exc_info=True)
        raise HTTPException(status_code=500, detail="查询失败,请稍后重试") from exc
    items = [_topic_view(t, queries) for t in topics]
    return {"run": run, "items": items, "total": len(items)}


def _in_scope(topic_scope: str | None, wanted: str) -> bool:
    if wanted == _SCOPE_ALL:
        return True
    if wanted == _SCOPE_ACTIONABLE:
        return topic_scope in _ACTIONABLE_SCOPES
    if wanted == _SCOPE_UNTRIAGED:
        return topic_scope is None
    return topic_scope == wanted


def _topic_view(topic: dict[str, Any], queries: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """主题行 → 对外形状:补上缺口提示与还查得到的样本提问。"""
    samples = [
        {
            # 落库时已脱敏;这里再过一遍,规则表后来新增的模式也能生效
            "query": mask_pii(str(queries[qid]["query"])),
            "asked_at": queries[qid]["created_at"],
            "injection_suspect": looks_like_instruction(str(queries[qid]["query"])),
        }
        for qid in topic["sample_query_ids"]
        if qid in queries
    ][:_SAMPLES_SHOWN]
    view = {k: v for k, v in topic.items() if k != "sample_query_ids"}
    view["gap_hint"] = gap_hint(int(topic["near_miss_count"]), int(topic["query_count"]))
    view["samples"] = samples
    return view
