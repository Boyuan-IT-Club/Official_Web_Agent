"""初筛管理 API:/admin/evaluation——初筛触发与 job 面。

权限:resume:audit(评审触发初筛;与 kb:manage/
agent:monitor 一样走 JWT permission_codes 自校)。
审计双录:触发写 agent_audit_log(生成完成审计在 runner)。
"""

import asyncio
import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from official_agent.evaluation import runner as eval_runner
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.state import audit, evaluation
from official_agent.state import qbank as qbank_store
from official_agent.web.routes import _authenticate

router = APIRouter()


async def _require_resume_audit(
    request: Request, authorization: Annotated[str | None, Header()] = None
):
    """初筛管理认证:JWT → resolve → permission_codes 含 resume:audit。"""
    identity, _ = await _authenticate(request, authorization)
    codes = identity.get("permission_codes") or []
    if "resume:audit" not in codes:
        raise HTTPException(status_code=403, detail="需要 resume:audit 权限")
    return identity


async def _require_evaluation_run(
    request: Request, authorization: Annotated[str | None, Header()] = None
):
    """初筛执行权:与 resume:audit(查看权)解耦的独立权限码。

    触发/重试 AI 初筛必须持 evaluation:run(后端 V46:仅超管与管理员
    授予;面试官/普通审核员不持有);前端按钮显隐用同一权限码,双侧
    拒绝语义一致。Backend 侧简历状态 6 写入同样验此码(服务账号)。"""
    identity, _ = await _authenticate(request, authorization)
    codes = identity.get("permission_codes") or []
    if "evaluation:run" not in codes:
        raise HTTPException(status_code=403, detail="需要 evaluation:run 权限")
    return identity


class RunBody(BaseModel):
    """触发初筛:单份或批量。只收 resume_id,user_id 由后端权威派生。"""

    cycle_id: int = Field(ge=1)
    items: list[eval_runner.TriggerItem] = Field(min_length=1, max_length=200)


@router.post("/admin/evaluation/run", status_code=202)
async def run_evaluation_jobs(
    body: RunBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_evaluation_run)],
) -> dict[str, Any]:
    """触发初筛(异步执行):返回 job_ids;结果落 evaluation_scorecard。"""
    try:
        job_ids = await eval_runner.get_runner().submit(
            body.cycle_id,
            list(body.items),  # TriggerItem 只含 resume_id
            trigger_user_id=int(identity.get("user_id") or 0),
        )
    except evaluation.JobStoreError as exc:
        # DB 侧建 job 失败是服务端故障,别混进下面的"调用方数据错位"
        raise HTTPException(status_code=500, detail="提交初筛任务失败,请稍后重试") from exc
    except eval_runner.SubmissionDataError as exc:
        # 调用方数据错位(领域异常)→ 400;其余 RuntimeError 等意外异常
        # 落到下面的兜底 500——不再被误判成调用方问题
        raise HTTPException(status_code=400, detail=f"简历归属核对失败:{exc}") from exc
    except Exception as exc:  # noqa: BLE001 — 统一 500 固定文案
        raise HTTPException(status_code=500, detail="提交初筛任务失败,请稍后重试") from exc
    return {"job_ids": job_ids, "submitted": len(job_ids)}


@router.get("/admin/evaluation/jobs")
async def list_evaluation_jobs(
    _: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    cycle_id: int,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """job 执行面列表(0 分队列按 scorecard.hard_zero 过滤呈现)。

    分页:超过一页的更早 job 靠 offset 回看,否则永远看不到。
    """
    try:
        jobs = await asyncio.to_thread(
            evaluation.list_jobs, cycle_id, status=status, limit=limit, offset=offset
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询 job 失败,请稍后重试") from exc
    return {"items": jobs, "total": len(jobs), "limit": limit, "offset": offset}


@router.post("/admin/evaluation/jobs/retry")
async def retry_failed_jobs(
    identity: Annotated[ResolvedIdentity, Depends(_require_evaluation_run)],
    cycle_id: int,
    include_stale: bool = False,
) -> dict[str, Any]:
    """失败 job 批量重试;include_stale=true 时连进程重启残留一起恢复。"""
    try:
        if include_stale:
            job_ids = await eval_runner.get_runner().retry_stale(cycle_id)
        else:
            job_ids = await eval_runner.get_runner().retry_failed(cycle_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="重试失败,请稍后重试") from exc
    return {"retried": job_ids}


# ── 预置题库(/admin/evaluation/qbank):面试官挑题面 ──


def _require_any(*codes: str):
    """任一权限码通过即放行(qbank 面:面试官 interview:evaluate / 评审 resume:audit)。"""

    async def _dep(request: Request, authorization: Annotated[str | None, Header()] = None):
        identity, _ = await _authenticate(request, authorization)
        owned = identity.get("permission_codes") or []
        if not any(c in owned for c in codes):
            raise HTTPException(status_code=403, detail=f"需要 {' 或 '.join(codes)} 权限")
        return identity

    return _dep


class PickBody(BaseModel):
    resume_id: int = Field(ge=1)
    cycle_id: int = Field(ge=1)
    schedule_id: int | None = None
    questions: list[dict[str, Any]] = Field(min_length=1, max_length=20)


def _qbank_missing_detail(job: dict[str, Any] | None) -> str:
    """题库行缺席且**不在生成中**时的 404 detail。

    生成中不走这里:get_qbank 对 pending/running 返回 200 + generating
    标记,让前端渲染"出题中"状态而不是报错。其余场合区分"从未跑过"
    与"跑了但题库线失败"——生产曾把权限 403 展示成"暂无题库",
    看似没数据、实则生成失败,文案必须给出真实原因与出路。"""
    if job is None:
        return "该候选暂无预置题库:尚未跑过 AI 初筛,请在简历审核触发"
    if str(job.get("qbank_status") or "") == "failed":
        reason = str(job.get("qbank_error") or "").strip()
        if reason:
            return f"该候选的预置题库生成失败:{reason}。请在简历审核重新触发 AI 初筛"
        return (
            "该候选的预置题库生成失败(原因未记录,旧版本 job 不落原因)。"
            "请在简历审核重新触发 AI 初筛;持续失败请查 agent 日志 qbank_done 事件"
        )
    return "该候选暂无预置题库(数据异常:job 已完成但题库缺失)。请在简历审核重新触发 AI 初筛"


@router.get("/admin/evaluation/qbank")
async def get_qbank(
    identity: Annotated[
        ResolvedIdentity, Depends(_require_any("interview:evaluate", "resume:audit"))
    ],
    resume_id: int,
    cycle_id: int,
) -> dict[str, Any]:
    """某候选最新预置题库(面试官面试前预查/打分工作台抽屉)。

    题库行缺席时联查最新 job 区分"从未跑过"与"跑了但题库线失败":
    生产曾把权限 403 炸掉的题库线展示成"暂无题库",看似没数据、
    实则生成失败——404 文案必须给出真实原因与出路。"""
    try:
        row = await asyncio.to_thread(qbank_store.latest_qbank, resume_id, cycle_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询题库失败,请稍后重试") from exc
    if row is None:
        job = await asyncio.to_thread(evaluation.latest_job, resume_id, cycle_id)
        if job and str(job.get("status") or "") in ("pending", "running"):
            # 生成中不是错:200 + generating 标记,前端渲染"出题中"状态。
            # 404 会触发前端报错路径——用户在出题窗口内打开抽屉是常态。
            return {"resume_id": resume_id, "cycle_id": cycle_id, "generating": True}
        raise HTTPException(status_code=404, detail=_qbank_missing_detail(job))
    envelope = row.get("envelope") or {}
    row = dict(row)
    # v2 题组的可挑题扁平视图(UI 挑题不感知组内嵌套)
    # 带 resume/cycle/版本生成稳定 ref_id,pick 时服务端权威绑定
    row["pickable"] = qbank_store.flatten_v2_pickable(
        envelope,
        resume_id=int(row.get("resume_id") or resume_id),
        cycle_id=int(row.get("cycle_id") or cycle_id),
        qbank_version=int(row.get("qbank_version") or 0),
    )
    return row


@router.post("/admin/evaluation/qbank/pick", status_code=201)
async def pick_questions(
    body: PickBody,
    identity: Annotated[
        ResolvedIdentity, Depends(_require_any("interview:evaluate", "resume:audit"))
    ],
) -> dict[str, Any]:
    """记录面试官实际勾选的题(pick log;候选人永不可见)。

    客户端引用只作"意图",落库的是服务端按当前题库解析出的权威
    引用(含 ref_id/定位索引)——同文题、过期题库、伪造证据路径在解析
    阶段直接 422,不再按题文反查串源。"""
    try:
        resolved = await asyncio.to_thread(
            qbank_store.resolve_picks, body.resume_id, body.cycle_id, body.questions
        )
    except LookupError as exc:
        # resolve_picks 的受控领域文案(指出哪条引用无效),面向管理面 422
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="查询题库失败,请稍后重试") from exc
    # 整批单事务:逐条提交时中途失败会让前端重试把已落库的题重复写进
    # pick log(该表无唯一约束),反哺出题的证据被污染
    try:
        ids = await asyncio.to_thread(
            qbank_store.record_picks,
            resume_id=body.resume_id,
            cycle_id=body.cycle_id,
            interviewer_user_id=int(identity.get("user_id") or 0),
            question_refs=resolved,
            schedule_id=body.schedule_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="记录勾选失败,请稍后重试") from exc
    return {"picked": len(ids)}


@router.get("/admin/evaluation/qbank/picks")
async def list_question_picks(
    _: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    resume_id: int,
    cycle_id: int,
) -> dict[str, Any]:
    """勾选记录(resume:audit 管理面;反哺出题覆盖率分析)。"""
    try:
        picks = await asyncio.to_thread(qbank_store.list_picks, resume_id, cycle_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询勾选失败,请稍后重试") from exc
    return {"items": picks, "total": len(picks)}


# ── 评审队列:0 分队列/采纳/改分/驳回 ──────────────────────


class AdoptBody(BaseModel):
    """采纳(AI 参考分或人工改分,以评审本人一票 upsert 后端多人打分)。

    score 必填:采纳 AI 参考总分传回其 total,改分则传人工分——Agent 不
    帮任何人决定终分。
    """

    resume_id: int = Field(ge=1)
    cycle_id: int = Field(ge=1)
    score: int = Field(ge=0, le=100)
    version: int | None = None  # 缺省采纳最新版


class RejectBody(BaseModel):
    resume_id: int = Field(ge=1)
    cycle_id: int = Field(ge=1)
    version: int | None = None


@router.get("/admin/evaluation/queue")
async def evaluation_queue(
    _: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    cycle_id: int,
    queue: str = "all",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """评审队列:queue=zero → 初筛不过(hard_zero)子队列;all → 全部评分卡。

    每项带 decided_status/decided_version:复评会写一张新 draft 卡把
    `status` 顶回 draft,这对字段保留了该简历历史上的人工决策,前端据此
    区分"没看过"与"看过已采纳/已驳回"。
    """
    if queue not in ("zero", "all"):
        raise HTTPException(status_code=400, detail="queue 只支持 zero/all")

    try:
        items = await asyncio.to_thread(
            evaluation.list_review_queue, cycle_id, queue, limit=limit, offset=offset
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="查询队列失败,请稍后重试") from exc
    return {
        "items": items,
        "total": len(items),
        "queue": queue,
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/evaluation/scorecard")
async def get_scorecard_for_review(
    identity: Annotated[
        ResolvedIdentity,
        Depends(_require_any("interview:evaluate", "resume:audit")),
    ],
    resume_id: int,
    cycle_id: int,
) -> dict[str, Any]:
    """单候选评分卡:面试官(interview:evaluate)场景内只读维卡。"""
    row = await asyncio.to_thread(evaluation.latest_scorecard, resume_id, cycle_id)
    if row is None:
        raise HTTPException(status_code=404, detail="该候选暂无评分卡")
    return row


@router.post("/admin/evaluation/adopt")
async def adopt_scorecard(
    body: AdoptBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """采纳 = 评审本人以自身身份向后端投一票(AI 不占 scorer)。

    原子/幂等语义:跨 Backend/审计/卡库三面没有事务,靠顺序 +
    如实上报保证"管理员收到的结果 == 实际状态":
    1. 验卡在前:无卡/版本不存在 → 404,此时零副作用;
    2. 意图审计在前(ADR-0006):失败 → 503"未执行"——确实什么都没发生;
    3. 投票按评审人 upsert:同票重试覆盖,不会双票;
    4. 投票落地后的失败如实说"票已投"(500),绝不再谎报"未执行";
    5. 结果审计 fail-open 但可见:响应带 audit_recorded,缺记录必须显眼。
    """
    from official_agent.tools.client import BackendError
    from official_agent.tools.readonly import get_backend_client as _gbc

    token = (authorization or "").removeprefix("Bearer ").strip()
    # 1) 验卡:干净失败路径,任何副作用都没发生
    versions = await asyncio.to_thread(evaluation.list_scorecards, body.resume_id, body.cycle_id)
    if body.version is None:
        if not versions:
            raise HTTPException(status_code=404, detail="该候选暂无评分卡,无法采纳")
        version = int(versions[0]["card_version"])  # list_scorecards 为版本倒序
    else:
        if not any(int(v["card_version"]) == body.version for v in versions):
            raise HTTPException(status_code=404, detail="指定评分卡版本不存在")
        version = body.version

    # 本次采纳的审计关联键:意图/结果两条记录共用,便于缺口配对
    audit_thread = f"eval:{body.cycle_id}:{secrets.token_hex(4)}"

    # 2) 意图审计(fail-closed):审计失败 → 503,采纳未执行,可安全重试
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=audit_thread,
            acting_user_id=int(identity.get("user_id") or 0),
            channel="evaluation",
            agent="evaluation-reviewer",
            action={
                "op": "adopt_vote_cast",
                "resume_id": body.resume_id,
                "cycle_id": body.cycle_id,
                "score": body.score,
            },
            decision=f"u{identity.get('user_id')}:adopt",
            result="评审采纳受理(先审计后投票,终分=多人平均)",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="审计服务不可用,采纳未执行,请稍后重试") from exc

    # 3) 投票(评审本人令牌)。后端 updateResumeScore 按 (resume, scorer)
    #    upsert 一人一票(uk_resume_scorer,已对后端源码证实),
    #    超时后重试覆盖同票、不会双票。502 文案不谎报"未送达":超时路径
    #    可能后端已落票,结果未知,如实说。
    try:
        client = await _gbc()
        await client.put_as_user(
            f"/api/resumes/{body.resume_id}/score",
            json={"score": body.score},
            user_token=token,
        )
    except BackendError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"投票未确认送达后端({type(exc).__name__});若后端已落票,同一评审人"
                "重试为覆盖同票。请重试或刷新查看实际状态"
            ),
        ) from exc

    # 4) 卡态迁移:投票已(至少可能已)落地,失败必须如实说"票已投"
    try:
        changed = await asyncio.to_thread(
            evaluation.set_scorecard_status,
            body.resume_id,
            body.cycle_id,
            version,
            "adopted",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="投票已送达后端但卡态更新出错,请重试采纳(同一评审人重试为覆盖投票)",
        ) from exc
    if not changed:
        raise HTTPException(
            status_code=500,
            detail="投票已送达但卡态更新失败,请重试采纳(同一评审人重试为覆盖投票)",
        )

    # 5) 结果审计(fail-open 但可见):投票已不可撤销,不阻断采纳结果
    audit_recorded = True
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=audit_thread,
            acting_user_id=int(identity.get("user_id") or 0),
            channel="evaluation",
            agent="evaluation-reviewer",
            action={
                "op": "adopt_scorecard",
                "resume_id": body.resume_id,
                "cycle_id": body.cycle_id,
                "version": version,
                "score": body.score,
            },
            decision=f"u{identity.get('user_id')}:adopt",
            result=f"评审采纳为一票(score={body.score});终分=多人平均",
        )
    except Exception:
        audit_recorded = False
        logging.getLogger(__name__).warning(
            "采纳结果审计写入失败(resume=%s,cycle=%s)——票已投、卡已置 adopted,审计缺口需人工补记",
            body.resume_id,
            body.cycle_id,
            exc_info=True,
        )
    return {
        "resume_id": body.resume_id,
        "cycle_id": body.cycle_id,
        "version": version,
        "status": "adopted",
        "score": body.score,
        "audit_recorded": audit_recorded,
    }


@router.post("/admin/evaluation/reject")
async def reject_scorecard(
    body: RejectBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_resume_audit)],
) -> dict[str, Any]:
    """驳回:卡置 rejected(AI 参考分不采纳);可复评(run 生成新版本)。"""
    # 验卡在前(显式传不存在 version 时不得先落审计再 404)
    versions = await asyncio.to_thread(evaluation.list_scorecards, body.resume_id, body.cycle_id)
    if body.version is None:
        if not versions:
            raise HTTPException(status_code=404, detail="该候选暂无评分卡,无法驳回")
        version = int(versions[0]["card_version"])
    else:
        if not any(int(v["card_version"]) == body.version for v in versions):
            raise HTTPException(status_code=404, detail="指定评分卡版本不存在")
        version = body.version
    # ADR-0006:驳回是写操作,先落审计(失败 → 503,不迁移)。
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"eval:{body.cycle_id}:{secrets.token_hex(4)}",
            acting_user_id=int(identity.get("user_id") or 0),
            channel="evaluation",
            agent="evaluation-reviewer",
            action={
                "op": "reject_scorecard",
                "resume_id": body.resume_id,
                "cycle_id": body.cycle_id,
                "version": version,
            },
            decision=f"u{identity.get('user_id')}:reject",
            result="评审驳回 AI 参考分(可复评)",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="审计服务不可用,驳回未执行,请稍后重试") from exc
    changed = await asyncio.to_thread(
        evaluation.set_scorecard_status,
        body.resume_id,
        body.cycle_id,
        version,
        "rejected",
    )
    if not changed:
        raise HTTPException(status_code=404, detail="评分卡不存在")
    return {
        "resume_id": body.resume_id,
        "cycle_id": body.cycle_id,
        "version": version,
        "status": "rejected",
    }
