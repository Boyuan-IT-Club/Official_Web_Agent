"""周期复盘管理 API:/admin/retrospective/*——指标快照的采集与读取,报告的生成、审阅与发布。

权限复用评估域现有的两个权限码,没有新增:
- evaluation:run(执行权,仅超管与管理员):触发快照采集、生成报告、看草稿、发布
- resume:audit(查看权):读快照,读**已发布**的报告。草稿是没人审过的模型产出,
  只给有执行权的人看;快照只有过了小格子抑制的聚合数,给有查看权的评审看不会
  落到个人

写的都是 agent 自己的表,对后端只读,所以不经确认令牌(那是后端写操作的闸)。
快照采集不是人工决策,采集人与时间记在快照行上;发布是人工决策,先写审计再执行
(审计写不进去就不发布)。

store 层同步 psycopg,路由内一律 asyncio.to_thread,不阻塞事件循环。
"""

import asyncio
import logging
import secrets
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from official_agent.config import get_settings
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.retrospective import metrics, reports
from official_agent.retrospective.catalog import METRICS_SCHEMA
from official_agent.retrospective.compare import SchemaMismatchError, with_baseline
from official_agent.state import audit
from official_agent.state import retrospective as store
from official_agent.state.retrospective import report_store
from official_agent.web.auth import require_any as _require_any

logger = logging.getLogger(__name__)

router = APIRouter()

_RUN_PERMISSION = "evaluation:run"
# 触发采集、生成与发布报告:与触发初筛同一个执行权
_require_evaluation_run = _require_any(_RUN_PERMISSION)
# 读取:与看评审队列同一个查看权;持执行权的人触发完要能看到结果
_require_read = _require_any("resume:audit", _RUN_PERMISSION)


class SnapshotBody(BaseModel):
    """触发一次指标快照采集。"""

    cycle_id: int = Field(ge=1)


class RunBody(BaseModel):
    """生成一版报告草稿;baseline_cycle_id 给了就对着那个周期写变化。"""

    cycle_id: int = Field(ge=1)
    baseline_cycle_id: int | None = Field(default=None, ge=1)


class PublishBody(BaseModel):
    cycle_id: int = Field(ge=1)
    version: int = Field(ge=1)


def _user_id(identity: ResolvedIdentity) -> int:
    return int(identity.get("user_id") or 0)


def _can_review_drafts(identity: ResolvedIdentity) -> bool:
    return _RUN_PERMISSION in (identity.get("permission_codes") or [])


# ── 指标快照 ────────────────────────────────────────────


def _snapshot_response(record: store.SnapshotRecord, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "cycle_id": record.cycle_id,
        "version": record.version,
        "metrics_schema": record.metrics_schema,
        "taken_by": record.taken_by,
        "taken_at": record.taken_at,
        "snapshot": body,
    }


async def _collect_and_save(cycle_id: int, taken_by: int) -> tuple[store.SnapshotRecord, bool]:
    """采集并保存快照,返回 (记录, 是否落了新版本);失败翻译成 HTTP 语义。"""
    try:
        payload = await metrics.collect_metrics(cycle_id)
    except metrics.CycleNotFoundError as exc:
        # 受控领域文案(只含周期 id),可直达管理面
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 未预期异常:原文只进日志,不出边界
        logger.warning("指标快照采集失败 cycle_id=%s", cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="采集指标失败,请稍后重试") from exc
    try:
        return await asyncio.to_thread(
            store.save_snapshot,
            cycle_id=cycle_id,
            metrics_schema=METRICS_SCHEMA,
            metrics=payload,
            taken_by=taken_by,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("指标快照保存失败 cycle_id=%s", cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="保存指标快照失败,请稍后重试") from exc


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


async def _load_baseline(
    record: store.SnapshotRecord, baseline_cycle_id: int
) -> store.SnapshotRecord:
    """基线周期最新的同版本快照;没有、或只有别的指标定义版本的,按原因报错。"""
    if baseline_cycle_id == record.cycle_id:
        raise HTTPException(status_code=400, detail="基线周期不能是本周期")
    baseline = await _load_snapshot(baseline_cycle_id, metrics_schema=record.metrics_schema)
    if baseline is not None:
        return baseline
    if await _load_snapshot(baseline_cycle_id) is None:
        raise HTTPException(status_code=404, detail="基线周期还没有指标快照")
    raise HTTPException(
        status_code=409,
        detail=f"基线周期没有 {record.metrics_schema} 版指标定义的快照,不能对比",
    )


def _compare(record: store.SnapshotRecord, baseline: store.SnapshotRecord) -> dict[str, Any]:
    try:
        return with_baseline(record.metrics, baseline.metrics)
    except SchemaMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    record, created = await _collect_and_save(body.cycle_id, _user_id(identity))
    return {**_snapshot_response(record, record.metrics), "created": created}


@router.get("/admin/retrospective/snapshot")
async def get_snapshot(
    _: Annotated[ResolvedIdentity, Depends(_require_read)],
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
    baseline = await _load_baseline(record, baseline_cycle_id)
    return _snapshot_response(record, _compare(record, baseline))


@router.get("/admin/retrospective/snapshots")
async def list_snapshots(
    _: Annotated[ResolvedIdentity, Depends(_require_read)],
    cycle_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """一个周期的全部快照版本(新的在前),不含指标正文;带各域采集状态。"""
    try:
        items = await asyncio.to_thread(store.list_snapshots, cycle_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("指标快照列表查询失败 cycle_id=%s", cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询指标快照失败,请稍后重试") from exc
    return {"items": items, "total": len(items)}


# ── 报告 ────────────────────────────────────────────────


@router.post("/admin/retrospective/run", status_code=202)
async def run_report(
    body: RunBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_evaluation_run)],
) -> dict[str, Any]:
    """生成一版报告草稿(后台执行):先采一次快照,再让分析员与撰稿人读它。

    立即返回占到的版本号,状态 generating;几分钟后用 GET …/report 看结果
    (draft 或 failed)。同一周期同时只能有一份在生成,否则 409。
    报告生成默认关闭(配置 retrospective_report_enabled),没打开时 503,什么都不做。
    """
    if not get_settings().retrospective_report_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "复盘报告生成尚未开启:四份 prompt 要先在真实模型上跑过 retro_probes 与 "
                "retro_injection,再把 RETROSPECTIVE_REPORT_ENABLED 设为 true"
            ),
        )
    user_id = _user_id(identity)
    snapshot, _created = await _collect_and_save(body.cycle_id, user_id)
    baseline = None
    content = snapshot.metrics
    if body.baseline_cycle_id is not None:
        baseline = await _load_baseline(snapshot, body.baseline_cycle_id)
        content = _compare(snapshot, baseline)
    try:
        version = await asyncio.to_thread(
            report_store.start_report,
            cycle_id=body.cycle_id,
            metrics_schema=snapshot.metrics_schema,
            snapshot_version=snapshot.version,
            baseline_cycle_id=baseline.cycle_id if baseline else None,
            baseline_snapshot_version=baseline.version if baseline else None,
            created_by=user_id,
        )
    except report_store.ReportInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘报告占号失败 cycle_id=%s", body.cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="创建报告失败,请稍后重试") from exc
    reports.spawn_generation(cycle_id=body.cycle_id, version=version, snapshot=content)
    return {
        "cycle_id": body.cycle_id,
        "version": version,
        "status": report_store.STATUS_GENERATING,
        "snapshot_version": snapshot.version,
        "baseline_cycle_id": baseline.cycle_id if baseline else None,
    }


@router.get("/admin/retrospective/report")
async def get_report(
    identity: Annotated[ResolvedIdentity, Depends(_require_read)],
    cycle_id: Annotated[int, Query(ge=1)],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """读一版报告;不给 version 取最新一版。

    没有执行权的人只看得到已发布的版本:对他们来说草稿、失败的版本都不存在(404)。
    """
    published_only = not _can_review_drafts(identity)
    try:
        record = await asyncio.to_thread(
            report_store.get_report, cycle_id, version=version, published_only=published_only
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘报告读取失败 cycle_id=%s", cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询报告失败,请稍后重试") from exc
    if record is None:
        if published_only:
            missing = "该周期还没有已发布的报告"
        else:
            missing = "指定版本的报告不存在" if version else "该周期还没有报告"
        raise HTTPException(status_code=404, detail=missing)
    return asdict(record)


@router.get("/admin/retrospective/reports")
async def list_reports(
    identity: Annotated[ResolvedIdentity, Depends(_require_read)],
    cycle_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """一个周期的报告版本列表(新的在前),不含正文;没有执行权的人只看到已发布的。"""
    try:
        items = await asyncio.to_thread(
            report_store.list_reports, cycle_id, published_only=not _can_review_drafts(identity)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘报告列表查询失败 cycle_id=%s", cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询报告失败,请稍后重试") from exc
    return {"items": items, "total": len(items)}


@router.post("/admin/retrospective/publish")
async def publish_report(
    body: PublishBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_evaluation_run)],
) -> dict[str, Any]:
    """发布一个草稿版本;该周期原先已发布的那版转为 superseded。

    顺序与初筛的驳回一致:先验版本(不存在或不是草稿 → 此时零副作用),再写审计
    (写不进去 → 503,未发布),最后改状态。
    """
    user_id = _user_id(identity)
    try:
        current = await asyncio.to_thread(
            report_store.get_report, body.cycle_id, version=body.version
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘报告读取失败 cycle_id=%s", body.cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询报告失败,请稍后重试") from exc
    if current is None:
        raise HTTPException(status_code=404, detail="指定版本的报告不存在")
    if current.status != report_store.STATUS_DRAFT:
        raise HTTPException(
            status_code=409, detail=str(report_store.ReportNotPublishableError(current.status))
        )
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"retro:{body.cycle_id}:{secrets.token_hex(4)}",
            acting_user_id=user_id,
            channel="retrospective",
            agent="retrospective-admin",
            action={
                "op": "publish_retrospective",
                "cycle_id": body.cycle_id,
                "version": body.version,
            },
            decision=f"u{user_id}:publish",
            result="发布周期复盘报告",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="审计服务不可用,报告未发布,请稍后重试"
        ) from exc
    try:
        record = await asyncio.to_thread(
            report_store.publish_report, body.cycle_id, body.version, published_by=user_id
        )
    except report_store.ReportNotPublishableError as exc:
        # 验版本之后、发布之前状态被别人改了(并发发布)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("复盘报告发布失败 cycle_id=%s", body.cycle_id, exc_info=True)
        raise HTTPException(status_code=500, detail="发布报告失败,请稍后重试") from exc
    return {
        "cycle_id": record.cycle_id,
        "version": record.version,
        "status": record.status,
        "published_by": record.published_by,
        "published_at": record.published_at,
    }
