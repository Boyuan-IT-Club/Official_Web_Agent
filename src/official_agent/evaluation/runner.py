"""B2 执行组织(#126):进程内 asyncio task runner + job 状态表。

- 触发:管理员手动(单/批量),POST /admin/evaluation/run → submit()
- 方案A(评审闸门1):前端只认 resume_id,user_id/cycle_id 由后端权威派生,
  开工前断言"按 user_id 查回的 resume == 本 job resume",防止错位操作。
- 每 job 一个 asyncio task(信号量限并发,LLM 慢操作);状态全在
  evaluation_job 表,进程重启后 pending/running 残留由启动自动恢复
  (闸门3:lifespan 调 retry_stale 全量扫,超 10 分钟+attempts 未满才重排)
- AI 0 分 = 初筛不过特殊标记:卡内 hard_zero 落列,不自动拒(#135)
- 失败可重试:mark failed + requeue;审计走 agent_audit_log(#124 双录)
- 题库线独立状态(闸门6):qbank_status=succeeded/failed/skipped,
  评分成功题库失败 → job succeeded + qbank_status=failed,管理面可见。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass

from official_agent.config import get_effective_settings
from official_agent.evaluation.github_client import normalize_github_login
from official_agent.evaluation.graph import _prompt_version, run_evaluation
from official_agent.evaluation.scoring import FieldText
from official_agent.state import audit, evaluation
from official_agent.tools.readonly import get_backend_client

_MAX_CONCURRENCY = 4


@dataclass(frozen=True)
class TriggerItem:
    """初筛触发项。方案A(评审闸门1):只认 resume_id 一个事实源。

    user_id 不再由前端携带——开工前由 fetch_resume_authority 从后端
    按简历号派生权威归属,杜绝"A 标 6、B 被评分"的错位。
    """

    resume_id: int


def _write_eval_usage_log(
    *,
    job_id: int,
    job_user_id: int | None,
    resume_id: int,
    cycle_id: int,
    qbank_version: int,
    usage: dict,
) -> None:
    """eval 通道用量日志(#154/D9):thread_id 关联 job;四列 token 进 M6 面板。

    fail-open:日志写失败不拖垮 job(与审计同语义)。"""
    try:
        from official_agent.state.conversation import write_conversation

        write_conversation(
            thread_id=f"eval:{job_id}",
            user_id=job_user_id,
            channel="evaluation",
            user_message=f"简历 {resume_id} 初筛+题库(周期 {cycle_id})",
            reply_summary=f"题库 v{qbank_version} 落库",
            tools=["explore", "grilling"],
            **(usage or {}),
        )
    except Exception:  # noqa: BLE001 — 用量日志缺失可容忍
        logging.getLogger(__name__).warning("eval 用量日志写入失败", exc_info=True)


def _log_event(job_id: int, cycle_id: int, resume_id: int, stage: str, **fields: object) -> None:
    """评测线结构化事件日志(#183):单行 key=value,分钟级定位失败阶段。

    只记 id/阶段/耗时/版本/错误类别,绝不落简历原文或 PII;trace id 由
    job 级 turn-trace(set in _run_job)进 traceparent 与 Langfuse,
    与本行同源可关联。"""
    parts = [f"eval_event stage={stage} job_id={job_id} cycle_id={cycle_id}"]
    for k, v in fields.items():
        if v is not None:
            parts.append(f"{k}={v}")
    logging.getLogger(__name__).info(" ".join(parts) + f" resume_id={resume_id}")


async def _set_resume_status(resume_id: int, status: int) -> None:
    """简历状态位(#用户反馈):6=AI初筛中(瞬态),结束回落 2。

    走管理员 PUT /api/resumes/status/{id}/{status}(evaluation:run,服务账号
    可用);失败 fail-open——状态位缺失只影响展示,不影响初筛本身。"""
    try:
        client = await get_backend_client()
        await client.put(f"/api/resumes/status/{resume_id}/{status}")
    except Exception:  # noqa: BLE001 — 状态位缺失可容忍
        logging.getLogger(__name__).warning(
            "简历状态位更新失败(resume=%s,status=%s)", resume_id, status, exc_info=True
        )


async def fetch_candidate_github(user_id: int) -> str:
    """从候选档案取 github 登录名(D17/#149):GET /api/admin/profiles/{userId}
    → detail.github(地址或裸登录名)→ 归一化为登录名。

    评测提交认领(submissions)是第二来源,档案为空时暂不回退(诚实边界,
    见 #149 验收记录)。任何失败返回空串——github_key 缺失只关评测线,
    不拖垮评分与仓线。"""
    try:
        client = await get_backend_client()
        data = await client.get(f"/api/admin/profiles/{user_id}")
        return normalize_github_login(str((data or {}).get("github") or ""))
    except Exception:  # noqa: BLE001 — 档案不可读不挡 job,但留可诊断痕迹
        logging.getLogger(__name__).warning(
            "候选档案 github 取数失败(user=%s),评测线跳过", user_id, exc_info=True
        )
        return ""


async def fetch_scoring_fields(user_id: int, cycle_id: int) -> tuple[int, list[FieldText]]:
    """服务账号取简历详情,映射为打分维度(textarea 型字段)。

    对应 GET /api/resumes/admin/{userId}/{cycleId}(resume:view 权限走
    服务账号,与 B1 的 PII 纪律一致:脱敏由后端返回层+字段面决定,这里
    只取 textarea 型主观题)。返回 (resume_id, fields)。
    """
    client = await get_backend_client()
    data = await client.get(f"/api/resumes/admin/{user_id}/{cycle_id}")
    resume_id = int(data.get("resumeId") or 0)
    if resume_id <= 0:
        # 缺 resumeId 静默落 0 会产生查不到的幽灵卡(B2 评审 P2)
        raise RuntimeError("后端响应缺 resumeId,拒绝评分")
    fields = [
        FieldText(
            field_key=str(f.get("fieldKey") or ""),
            title=str(f.get("fieldLabel") or ""),
            value=str(f.get("fieldValue") or ""),
            placeholder=str(f.get("placeholder") or ""),
        )
        for f in data.get("simpleFields") or []
        if f.get("fieldType") == "textarea"
    ]
    return resume_id, fields


async def fetch_resume_authority(resume_id: int) -> dict:
    """方案A:按简历号向后端取权威 user_id/cycle_id(闸门1)。

    对应 Backend GET /api/resumes/admin/by-resume/{resumeId}(resume:view)。
    只认 resume_id 一个事实源——前端不再传 user_id,这里派生:
    - user_id:简历归属人(取数/审计用);
    - cycle_id:简历所属周期。
    返回后端 resumeId 必须 == 请求 resume_id,否则抛错(防错位)。
    """
    client = await get_backend_client()
    data = await client.get(f"/api/resumes/admin/by-resume/{resume_id}")
    resume_id_back = int(data.get("resumeId") or 0)
    if resume_id_back != resume_id:
        raise RuntimeError(
            f"简历权威归属不一致:请求 resume_id={resume_id},后端返回 {resume_id_back}——"
            "拒绝执行,防错位操作"
        )
    user_id = int(data.get("userId") or 0)
    if user_id <= 0:
        raise RuntimeError(f"后端未返回简历 {resume_id} 的归属 user_id,拒绝执行")
    return {
        "resume_id": resume_id,
        "user_id": user_id,
        "cycle_id": int(data.get("cycleId") or 0),
    }


def _mask_fields_for_model(fields: list[FieldText], *, resume_id: int) -> list[FieldText]:
    """#176 出口契约:简历字段进评分/出题模型前强制深度脱敏。

    打分面(FieldText docstring #123 契约)本就要求 value 已脱敏,但此前
    无强制——评估线绕过了 chat 工具返回层的 mask_pii_deep。这里在唯一
    入口(_run_job)统一执行:value 以 {字段键: 原文} 结构过 mask_pii_deep
    ——键级白名单管姓名类字段(姓名不进文本正则,#164),文本正则管
    手机/身份证/邮箱/QQ(学号等 5-11 位数字同规则)。命中打安全日志
    (只记数量与 resume_id,不落原文)。
    """
    from official_agent.security.pii import mask_pii_deep

    out: list[FieldText] = []
    hits = 0
    for f in fields:
        # placeholder 与 value 走同一键级掩(#176 评审):姓名类字段的
        # 「与 placeholder 同文」全等判在掩码后仍成立
        masked_value = mask_pii_deep([{f.field_key: f.value}])[0][f.field_key]
        masked_ph = mask_pii_deep([{f.field_key: f.placeholder}])[0][f.field_key]
        if masked_value != f.value:
            hits += 1
        out.append(
            FieldText(
                field_key=f.field_key,
                title=str(mask_pii_deep(f.title)),
                value=str(masked_value),
                placeholder=str(masked_ph),
            )
        )
    if hits:
        logging.getLogger(__name__).warning(
            "guard_event guard_name=eval_pii_exit verdict=masked fields=%d resume=%s",
            hits,
            resume_id,
        )
    return out


class EvaluationRunner:
    """进程内单例:提交/并发控制/重试。状态真源在 evaluation_job 表。"""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(_MAX_CONCURRENCY)
        # asyncio 只持任务弱引用:不保存会被 GC,job 静默卡死(B2 评审 P1)
        self._tasks: set[asyncio.Task] = set()
        # #193 幂等派发:进程内在跑 job 登记——create_jobs 幂等复用活跃 job_id
        # 后,submit/重试若再无条件派发会双跑同一简历。单 worker 语义;跨副本
        # 由 DB 唯一活跃索引 + attempts 上限兜底(扩副本前需外置,见 #172 map)。
        self._inflight: set[int] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _try_dispatch(self, job_id: int, cycle_id: int, *, trigger_user_id: int) -> bool:
        """同 job 进程内只派发一次(#193);返回是否实际派发。

        已在执行(含并发双击、重试双发、stale 恢复撞上在跑慢 job)→ 跳过;
        执行完成由守卫协程的 finally 清理登记,之后再触发是合法复评。"""
        if job_id in self._inflight:
            logging.getLogger(__name__).info("job %s 已在执行,跳过重复派发(#193 幂等)", job_id)
            return False
        self._inflight.add(job_id)

        async def _guarded() -> None:
            try:
                await self._run_job(job_id, cycle_id, trigger_user_id=trigger_user_id)
            finally:
                self._inflight.discard(job_id)

        try:
            self._spawn(_guarded())
        except Exception:
            # create_task 失败(如事件循环已关):登记必须回收,否则该 job
            # 在本进程内永久无法再派发(#193 评审 P3)
            self._inflight.discard(job_id)
            raise
        return True

    async def submit(
        self, cycle_id: int, items: list[TriggerItem], *, trigger_user_id: int
    ) -> list[int]:
        if not items:
            return []
        # 方案A(闸门1):前端只给 resume_id,user_id 由后端按简历号权威派生。
        # 逐个核对返回的 resumeId == 请求 resume_id,不一致抛错(batch 全拒)。
        authoritative: list[tuple[int, int]] = []
        for item in items:
            authority = await fetch_resume_authority(item.resume_id)
            authoritative.append((authority["resume_id"], authority["user_id"]))
        job_ids = await asyncio.to_thread(
            evaluation.create_jobs,
            authoritative,
            cycle_id,
        )
        # 先派发后审计:审计失败不得让已建的 job 永远 pending(B2 E2E 实测)
        for job_id in job_ids:
            self._try_dispatch(job_id, cycle_id, trigger_user_id=trigger_user_id)
        try:
            await asyncio.to_thread(
                audit.write_audit,
                thread_id=f"eval:{cycle_id}:{secrets.token_hex(4)}",
                acting_user_id=trigger_user_id,
                channel="evaluation",
                agent="evaluation-runner",
                action={
                    "op": "run_initial_screening",
                    "cycle_id": cycle_id,
                    "resume_ids": [i.resume_id for i in items],
                },
                decision=f"u{trigger_user_id}:run",
                result=f"提交 {len(job_ids)} 个初筛 job",
            )
        except Exception:  # noqa: BLE001 — 合规记录丢失必须可见
            logging.getLogger(__name__).warning("触发审计写入失败(jobs=%s)", job_ids, exc_info=True)
        return job_ids

    async def _run_job(self, job_id: int, cycle_id: int, *, trigger_user_id: int) -> None:
        from official_agent.observability import (
            eval_job_trace_id,
            reset_turn_trace_id,
            set_turn_trace_id,
        )

        # #183:job 级 correlation id——Langfuse trace、出站 Backend 请求
        # traceparent、审计 trace_id、结构化日志四面同 id(确定性可复算)。
        trace_token = set_turn_trace_id(eval_job_trace_id(job_id))
        try:
            await self._execute_job(job_id, cycle_id, trigger_user_id=trigger_user_id)
        finally:
            reset_turn_trace_id(trace_token)

    async def _execute_job(self, job_id: int, cycle_id: int, *, trigger_user_id: int) -> None:
        from official_agent.observability import eval_job_trace_id

        started = time.monotonic()
        job = await asyncio.to_thread(evaluation.get_job, job_id)
        if job is None:
            return
        async with self._sem:
            await asyncio.to_thread(evaluation.mark_job, job_id, "running")
            resume_id = int(job["resume_id"])
            _log_event(
                job_id,
                cycle_id,
                resume_id,
                "started",
                attempts=int(job.get("attempts") or 0) + 1,
                model=get_effective_settings().model_strong,
            )
            # 用户反馈:简历状态加「AI初筛中」(瞬态 6),结束后回落 2——
            # 否则触发了初筛但状态无变化,让人困惑。
            # 注意:6 与回 2 只作用于权威 resume_id,不会误改别的简历。
            await _set_resume_status(resume_id, 6)
            card: dict | None = None
            version: int | None = None
            qbank_status = "skipped"
            try:
                t_eval = time.monotonic()
                fetched_resume_id, fields = await fetch_scoring_fields(job["user_id"], cycle_id)
                # 闸门1 硬断言:后端按 user_id+cycle 派生出的简历必须就是本 job
                # 的简历;不一致说明数据错位,立即失败,绝不带病继续。
                if fetched_resume_id != resume_id:
                    raise RuntimeError(
                        f"简历归属错位:job resume_id={resume_id},"
                        f"后端按 user_id={job['user_id']} 返回 {fetched_resume_id}——拒绝评分"
                    )
                # #176 出口契约:评分与出题两个模型入口共用这份脱敏后字段
                fields = _mask_fields_for_model(fields, resume_id=resume_id)
                scoring_usage: dict[str, int | None] = {}
                card = await run_evaluation(
                    [
                        {
                            "field_key": f.field_key,
                            "title": f.title,
                            "value": f.value,
                            "placeholder": f.placeholder,
                        }
                        for f in fields
                    ],
                    resume_id=resume_id,
                    cycle_id=cycle_id,
                    usage_out=scoring_usage,
                    correlation_id=eval_job_trace_id(job_id),
                )
                version = await asyncio.to_thread(
                    evaluation.save_scorecard,
                    card,
                    resume_id=resume_id,
                    cycle_id=cycle_id,
                    prompt_version=_prompt_version(),
                )
                _log_event(
                    job_id,
                    cycle_id,
                    resume_id,
                    "scorecard_saved",
                    card_version=version,
                    duration_ms=int((time.monotonic() - t_eval) * 1000),
                    prompt_version=_prompt_version(),
                    input_tokens=scoring_usage.get("input_tokens"),
                    output_tokens=scoring_usage.get("output_tokens"),
                )
                # 调查 bundle → qbank:题库线失败不再静默(闸门6 qbank_status),
                # job 记 succeeded + qbank_status=failed——评分卡有效,题库缺失
                # 管理面可见、可单独重试。
                t_qbank = time.monotonic()
                try:
                    from official_agent.evaluation import bundle as eval_bundle
                    from official_agent.state import qbank as qbank_store

                    envelope = await eval_bundle.run_bundle(
                        fields,
                        resume_id=resume_id,
                        cycle_id=cycle_id,
                        github_key=await fetch_candidate_github(job["user_id"]) or None,
                        github_token=get_effective_settings().github_token,
                    )
                    qbank_version = await asyncio.to_thread(
                        qbank_store.save_qbank,
                        resume_id=resume_id,
                        cycle_id=cycle_id,
                        source=(
                            str(envelope["groups"][0].get("group", "bundle"))
                            if envelope.get("groups")
                            else "bundle"
                        ),
                        envelope=envelope,
                        prompt_version=str(envelope.get("prompt_version", "")),
                    )
                    qbank_status = "succeeded"
                    # D9/#154:探索+出题用量进 conversation_log(evaluation 通道,
                    # 关联 job;复用 M6 #113 四列管道,不新建表)
                    usage_total = envelope.get("explore_usage_total") or {}
                    await asyncio.to_thread(
                        _write_eval_usage_log,
                        job_id=job_id,
                        job_user_id=job.get("user_id"),
                        resume_id=resume_id,
                        cycle_id=cycle_id,
                        qbank_version=qbank_version,
                        usage=usage_total,
                    )
                except Exception as qbank_exc:  # noqa: BLE001 — 题库线失败不拖垮评分卡
                    qbank_status = "failed"
                    _log_event(
                        job_id,
                        cycle_id,
                        resume_id,
                        "qbank_done",
                        status="failed",
                        duration_ms=int((time.monotonic() - t_qbank) * 1000),
                        error_class=type(qbank_exc).__name__,
                    )
                    logging.getLogger(__name__).warning(
                        "调查 bundle 落库失败(job=%s),job qbank_status=failed",
                        job_id,
                        exc_info=True,
                    )
                else:
                    _log_event(
                        job_id,
                        cycle_id,
                        resume_id,
                        "qbank_done",
                        status="succeeded",
                        qbank_version=qbank_version,
                        duration_ms=int((time.monotonic() - t_qbank) * 1000),
                    )
                await asyncio.to_thread(
                    evaluation.mark_job,
                    job_id,
                    "succeeded",
                    card_version=version,
                    qbank_status=qbank_status,
                )
            except Exception as exc:  # noqa: BLE001 — job 失败落表,可重试
                await _set_resume_status(resume_id, 2)
                await asyncio.to_thread(
                    evaluation.mark_job,
                    job_id,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                    qbank_status=qbank_status,
                )
                logging.getLogger(__name__).warning(
                    "eval job failed job=%s error_class=%s",
                    job_id,
                    type(exc).__name__,
                    exc_info=True,
                )
                _log_event(
                    job_id,
                    cycle_id,
                    resume_id,
                    "failed",
                    error_class=type(exc).__name__,
                    attempts=int(job.get("attempts") or 0) + 1,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                return
            _log_event(
                job_id,
                cycle_id,
                resume_id,
                "succeeded",
                card_version=version,
                qbank_status=qbank_status,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            # 6 是瞬态:成功路径在审计前回落 2(失败路径已回落),
            # 否则简历永久卡"初筛中"(闸门3)。
            await _set_resume_status(resume_id, 2)
            # 完成审计在保护段外:审计失败不得把已 succeeded 的 job 翻成 failed
            try:
                await asyncio.to_thread(
                    audit.write_audit,
                    thread_id=f"eval:{cycle_id}:{secrets.token_hex(4)}",
                    acting_user_id=trigger_user_id,  # 谁触发谁进审计(ADR-0006)
                    channel="evaluation",
                    agent="evaluation-runner",
                    action={
                        "op": "scorecard_generated",
                        "resume_id": resume_id,
                        "cycle_id": cycle_id,
                        "candidate_user_id": job["user_id"],
                        "hard_zero": bool(card.get("hard_zero")) if card else None,
                        "qbank_status": qbank_status,
                    },
                    decision="system:auto",
                    result=(
                        f"卡 v{version},总分 {card.get('total')}(AI 参考分,未写 resume_score)"
                        if card
                        else "生成完成"
                    ),
                )
            except Exception:  # noqa: BLE001 — 审计失败只记日志,不影响 job 态
                logging.getLogger(__name__).warning(
                    "完成审计写入失败(job=%s)", job_id, exc_info=True
                )

    async def retry_failed(self, cycle_id: int) -> list[int]:
        """失败 job 全部重回 pending 并重新派发;返回重派 job_ids。"""
        job_ids = await asyncio.to_thread(evaluation.requeue_failed, cycle_id)
        for job_id in job_ids:
            self._try_dispatch(job_id, cycle_id, trigger_user_id=0)
        return job_ids

    async def retry_stale(self, cycle_id: int) -> list[int]:
        """残留恢复:超时限的 failed/pending/running 未超上限 job 回 pending 并派发。

        闸门3:启动时与手动(include_stale)共用;attempts 达上限的 job 不重排。
        """
        job_ids = await asyncio.to_thread(evaluation.requeue_stale, cycle_id)
        for job_id in job_ids:
            self._try_dispatch(job_id, cycle_id, trigger_user_id=0)
        return job_ids

    async def recover_stale_on_startup(self, *, older_than_minutes: int = 10) -> list[int]:
        """闸门3 启动自动恢复:全量扫残留(不限周期),重派未超上限的僵 job。

        requeue_stale_all_cycles 返回 [{job_id, cycle_id}](#175):恢复必须在
        **原 cycle** 派发——曾把整行 dict 当 job_id、cycle 硬编码 0,多周期
        数据下恢复必错位。返回重派的 job_id 列表。
        """
        rows = await asyncio.to_thread(
            evaluation.requeue_stale_all_cycles,
            older_than_minutes=older_than_minutes,
        )
        for row in rows:
            self._try_dispatch(int(row["job_id"]), int(row["cycle_id"]), trigger_user_id=0)
        return [int(row["job_id"]) for row in rows]


_runner: EvaluationRunner | None = None


def get_runner() -> EvaluationRunner:
    """进程内单例(测试可 set_runner 注入替身)。"""
    global _runner
    if _runner is None:
        _runner = EvaluationRunner()
    return _runner


def set_runner(runner: EvaluationRunner | None) -> None:
    global _runner
    _runner = runner
