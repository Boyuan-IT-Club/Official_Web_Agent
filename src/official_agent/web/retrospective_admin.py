"""周期复盘管理 API:/admin/retrospective/snapshot*——触发指标快照采集、读取快照。

- 权限复用评估域现有的两个权限码:触发采集要 evaluation:run(执行权,仅超管与
  管理员);读取要 resume:audit(查看权),能触发的人也能读。快照里只有过了
  小格子抑制的聚合数,给有查看权的评审看不会落到个人
- 采集对后端只读、只写 agent 自己的快照表,所以不经确认令牌(那是后端写操作的闸);
  谁在什么时候采的记在快照行上(taken_by / taken_at)
- store 层同步 psycopg,路由内一律 asyncio.to_thread,不阻塞事件循环
"""

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from official_agent.graphs.identity import ResolvedIdentity
from official_agent.retrospective import metrics
from official_agent.retrospective.catalog import METRICS_SCHEMA
from official_agent.retrospective.compare import SchemaMismatchError, with_baseline
from official_agent.state import retrospective as store
from official_agent.web.auth import require_any as _require_any

logger = logging.getLogger(__name__)

router = APIRouter()

# 触发采集:与触发初筛同一个执行权
_require_evaluation_run = _require_any("evaluation:run")
# 读取快照:与看评审队列同一个查看权;持执行权的人触发完要能看到结果
_require_snapshot_read = _require_any("resume:audit", "evaluation:run")


class SnapshotBody(BaseModel):
    """触发一次指标快照采集。"""

    cycle_id: int = Field(ge=1)


def _snapshot_response(record: store.SnapshotRecord, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": record.cycle_id,
        "version": record.version,
        "metrics_schema": record.metrics_schema,
        "taken_by": record.taken_by,
        "taken_at": record.taken_at,
        "snapshot": body,
    }


@router.post("/admin/retrospective/snapshot")
async def take_snapshot(
    body: SnapshotBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_evaluation_run)],
) -> dict[str, Any]:
    """采集并保存一个周期的指标快照。

    重复触发是幂等的:算出来的内容与该周期最新一版相同就不落新版本,返回已有的
    那一版,created 为 false;内容变了才多一版,旧版本保留。
    某个数据源不可用时照样落快照,缺席的域在 snapshot.domains 里标明——看到
    unavailable 就等数据源恢复后再触发一次。
    """
    try:
        payload = await metrics.collect_metrics(body.cycle_id)
    except metrics.CycleNotFoundError as exc:
        # 受控领域文案(只含周期 id),可直达管理面
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 未预期异常:原文只进日志,不出边界
        logger.warning("指标快照采集失败 cycle_id=%s", body.cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="采集指标失败,请稍后重试") from exc
    try:
        record, created = await asyncio.to_thread(
            store.save_snapshot,
            cycle_id=body.cycle_id,
            metrics_schema=METRICS_SCHEMA,
            metrics=payload,
            taken_by=int(identity.get("user_id") or 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("指标快照保存失败 cycle_id=%s", body.cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="保存指标快照失败,请稍后重试") from exc
    return {**_snapshot_response(record, record.metrics), "created": created}


@router.get("/admin/retrospective/snapshot")
async def get_snapshot(
    _: Annotated[ResolvedIdentity, Depends(_require_snapshot_read)],
    cycle_id: Annotated[int, Query(ge=1)],
    version: Annotated[int | None, Query(ge=1)] = None,
    baseline_cycle_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """读一个周期的指标快照;不给 version 取最新一版。

    给了 baseline_cycle_id 就对着那个周期最新的同版本快照算差值(每个指标多出
    prev / delta / delta_ratio)。哪个周期算「上一届」由调用方指定:周期 id 和
    日期都说明不了(测试周期、一年多次招新)。指标定义版本不同则 409,不硬比。
    """
    record = await _load_snapshot(cycle_id, version=version)
    if record is None:
        missing = "指定版本的指标快照不存在" if version else "该周期还没有指标快照"
        raise HTTPException(status_code=404, detail=missing)
    if baseline_cycle_id is None:
        return _snapshot_response(record, record.metrics)
    if baseline_cycle_id == cycle_id:
        raise HTTPException(status_code=400, detail="基线周期不能是本周期")
    baseline = await _load_snapshot(baseline_cycle_id, metrics_schema=record.metrics_schema)
    if baseline is None:
        if await _load_snapshot(baseline_cycle_id) is None:
            raise HTTPException(status_code=404, detail="基线周期还没有指标快照")
        raise HTTPException(
            status_code=409,
            detail=f"基线周期没有 {record.metrics_schema} 版指标定义的快照,不能对比",
        )
    try:
        compared = with_baseline(record.metrics, baseline.metrics)
    except SchemaMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _snapshot_response(record, compared)


@router.get("/admin/retrospective/snapshots")
async def list_snapshots(
    _: Annotated[ResolvedIdentity, Depends(_require_snapshot_read)],
    cycle_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """一个周期的全部快照版本(新的在前),不含指标正文;带各域采集状态。"""
    try:
        items = await asyncio.to_thread(store.list_snapshots, cycle_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("指标快照列表查询失败 cycle_id=%s", cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询指标快照失败,请稍后重试") from exc
    return {"items": items, "total": len(items)}


async def _load_snapshot(
    cycle_id: int, *, version: int | None = None, metrics_schema: str | None = None
) -> store.SnapshotRecord | None:
    try:
        return await asyncio.to_thread(
            store.get_snapshot, cycle_id, version=version, metrics_schema=metrics_schema
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("指标快照读取失败 cycle_id=%s", cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询指标快照失败,请稍后重试") from exc
