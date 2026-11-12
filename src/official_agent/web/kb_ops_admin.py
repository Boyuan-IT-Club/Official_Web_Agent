"""知识库运营管理 API:/api/agent/admin/kb/ops* —— 离线分析、主题清单、草稿审核、忠实度抽样。

- 权限:``kb:manage``,与知识条目维护同一个权限码——能改知识库的人才需要看它缺什么
- 路由由 kb_admin 的 router 一并注册,不单独挂到 app
- 主题与草稿的样本提问按检索日志行 id 回查:所在会话已删除的样本不再出现
- 只有「批准草稿」会向知识库写内容,而且只在管理员明确点了批准之后:
  同一份草稿的批准由锁串行化;先落意图审计,写不进就不发布(fail-closed,
  ADR-0005/0006);发布是幂等的,中途失败如实上报、可以重试
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from official_agent.graphs.identity import ResolvedIdentity
from official_agent.kb import query_log
from official_agent.kb import store as kb_store
from official_agent.kb.embedding import EmbeddingError, EmbeddingNotConfiguredError
from official_agent.kb_ops import faithfulness_runner, publish
from official_agent.kb_ops import runner as kb_ops_runner
from official_agent.kb_ops.cluster import gap_hint
from official_agent.kb_ops.triage import looks_like_instruction
from official_agent.security.pii import mask_pii
from official_agent.state import audit
from official_agent.state import kb_faithfulness as faithfulness_state
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


def _samples(query_ids: list[int], queries: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """样本引用 → 还查得到的样本提问,至多 _SAMPLES_SHOWN 条。"""
    return [
        {
            # 落库时已脱敏;这里再过一遍,规则表后来新增的模式也能生效
            "query": mask_pii(str(queries[qid]["query"])),
            "asked_at": queries[qid]["created_at"],
            "injection_suspect": looks_like_instruction(str(queries[qid]["query"])),
        }
        for qid in query_ids
        if qid in queries
    ][:_SAMPLES_SHOWN]


def _topic_view(topic: dict[str, Any], queries: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """主题行 → 对外形状:补上缺口提示与还查得到的样本提问。"""
    view = {k: v for k, v in topic.items() if k != "sample_query_ids"}
    view["gap_hint"] = gap_hint(int(topic["near_miss_count"]), int(topic["query_count"]))
    view["samples"] = _samples(topic["sample_query_ids"], queries)
    return view


# ── 草稿审核 ────────────────────────────────────────────

_DRAFT_STATUSES = ("pending", "approved", "rejected", "superseded")
_DRAFT_KINDS = ("alt_question", "new_faq", "missing")


class DraftEditBody(BaseModel):
    """批准前修改草稿。cycle_id 为 null = 发布成常青条目。"""

    question: str = Field(min_length=1, max_length=200)
    answer: str = Field(default="", max_length=4000)
    cycle_id: int | None = Field(default=None, ge=1)


def _draft_view(draft: dict[str, Any], queries: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """草稿行 → 对外形状:样本引用换成还查得到的样本提问。"""
    view = dict(draft)
    support = dict(draft.get("support") or {})
    view["samples"] = _samples(list(support.pop("sample_query_ids", [])), queries)
    view["support"] = support
    return view


async def _load_draft(draft_id: int) -> dict[str, Any]:
    try:
        draft = await asyncio.to_thread(kb_ops_state.get_draft, draft_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_ops 草稿查询失败 draft_id=%s", draft_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询失败,请稍后重试") from exc
    if draft is None:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return draft


def _require_pending(draft: dict[str, Any]) -> None:
    if draft["status"] != kb_ops_state.DRAFT_PENDING:
        raise HTTPException(status_code=409, detail=f"草稿已处理(当前状态 {draft['status']})")


@contextlib.asynccontextmanager
async def _draft_lock(draft_id: int) -> AsyncIterator[None]:
    """持有一份草稿的处理锁;别人正在处理它 → 409。

    修改、批准、驳回都要先拿这把锁:否则批准读到草稿之后、发布之前,另一个请求可以
    把它改掉或驳回,结果是发布了旧内容,或者内容已发布而草稿记成了驳回。
    """
    try:
        lock = await asyncio.to_thread(kb_ops_state.try_acquire_draft_lock, draft_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_ops 草稿锁获取失败 draft_id=%s", draft_id, exc_info=True)
        raise HTTPException(status_code=500, detail="操作失败,请稍后重试") from exc
    if lock is None:
        raise HTTPException(status_code=409, detail="这份草稿正在被处理,请稍后刷新")
    try:
        yield
    finally:
        await asyncio.to_thread(lock.close)


@router.get("/admin/kb/ops/drafts")
async def list_kb_ops_drafts(
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    status: str | None = kb_ops_state.DRAFT_PENDING,
    kind: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """草稿分页列表,新的在前。status 缺省只看待审的;传 all 看全部。"""
    if status == _SCOPE_ALL:
        status = None
    if status is not None and status not in _DRAFT_STATUSES:
        raise HTTPException(status_code=400, detail="status 取值无效")
    if kind is not None and kind not in _DRAFT_KINDS:
        raise HTTPException(status_code=400, detail="kind 取值无效")
    page, size = max(1, page), max(1, min(size, 100))
    try:
        result = await asyncio.to_thread(
            kb_ops_state.list_drafts, status=status, kind=kind, page=page, size=size
        )
        sample_ids = [
            qid
            for d in result["items"]
            for qid in (d.get("support") or {}).get("sample_query_ids", [])
        ]
        queries = await asyncio.to_thread(query_log.get_queries, sample_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_ops 草稿列表查询失败", exc_info=True)
        raise HTTPException(status_code=500, detail="查询失败,请稍后重试") from exc
    result["items"] = [_draft_view(d, queries) for d in result["items"]]
    return result


@router.get("/admin/kb/ops/drafts/{draft_id}")
async def get_kb_ops_draft(
    draft_id: int,
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """草稿详情。补问法草稿附带目标 FAQ 的现有内容(target),供对照。"""
    draft = await _load_draft(draft_id)
    try:
        sample_ids = list((draft.get("support") or {}).get("sample_query_ids", []))
        queries = await asyncio.to_thread(query_log.get_queries, sample_ids)
        target = None
        if draft.get("target_source_id"):
            target = await asyncio.to_thread(kb_store.get_source, draft["target_source_id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_ops 草稿详情查询失败 draft_id=%s", draft_id, exc_info=True)
        raise HTTPException(status_code=500, detail="查询失败,请稍后重试") from exc
    view = _draft_view(draft, queries)
    view["target"] = (
        {
            "source_id": target.source_id,
            "title": target.title,
            "question": target.question,
            "answer": target.answer,
            "alt_questions": target.alt_questions,
            "enabled": target.enabled,
        }
        if target is not None
        else None
    )
    return view


@router.put("/admin/kb/ops/drafts/{draft_id}")
async def edit_kb_ops_draft(
    draft_id: int,
    body: DraftEditBody,
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """批准前修改草稿的问题、答案与绑定周期。只有待审的草稿能改。

    改动只落在草稿上,不碰知识库;谁改过记在草稿的 edited_by 上,发布时进审计。
    """
    async with _draft_lock(draft_id):
        _require_pending(await _load_draft(draft_id))
        try:
            changed = await asyncio.to_thread(
                kb_ops_state.update_draft_content,
                draft_id,
                question=body.question.strip(),
                answer=body.answer.strip(),
                cycle_id=body.cycle_id,
                edited_by=int(identity.get("user_id") or 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb_ops 草稿修改失败 draft_id=%s", draft_id, exc_info=True)
            raise HTTPException(status_code=500, detail="保存失败,请稍后重试") from exc
    if not changed:
        raise HTTPException(status_code=409, detail="草稿已被处理,无法修改")
    return {"draft_id": draft_id, "status": kb_ops_state.DRAFT_PENDING}


def _draft_audit_action(op: str, draft: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """草稿审核的审计指纹:哪份草稿、什么类型、内容是哪些 prompt 版本产出的、人改没改过。"""
    return {
        "op": op,
        "draft_id": draft["draft_id"],
        "kind": draft["kind"],
        "question": draft["question"],
        "target_source_id": draft.get("target_source_id"),
        "prompt_versions": draft.get("prompt_versions") or {},
        "edited_by": draft.get("edited_by"),
        **extra,
    }


async def _write_draft_audit(
    user_id: int, draft: dict[str, Any], *, action: dict[str, Any], decision: str, result: str
) -> None:
    await asyncio.to_thread(
        audit.write_audit,
        thread_id=f"kbdraft:{draft['draft_id']}",
        acting_user_id=user_id,
        channel="kb_ops",
        agent="kb-ops-reviewer",
        action=action,
        decision=f"u{user_id}:{decision}",
        result=result,
    )


def _map_publish_error(exc: Exception) -> HTTPException:
    """发布失败 → HTTP 语义。这几类失败发生时知识库都没有被改动,草稿仍是待审。"""
    if isinstance(exc, kb_store.KbValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, EmbeddingNotConfiguredError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, EmbeddingError):
        return HTTPException(status_code=502, detail=str(exc))
    logger.warning("kb_ops 草稿发布未预期失败", exc_info=exc)
    return HTTPException(status_code=500, detail="发布失败,知识库未改动,请稍后重试")


@router.post("/admin/kb/ops/drafts/{draft_id}/approve")
async def approve_kb_ops_draft(
    draft_id: int,
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """批准草稿并发布到知识库。

    顺序与如实上报(跨审计、知识库、草稿表三处没有事务):
    1. 抢这份草稿的锁:有人正在处理同一份 → 409,不会发布两遍;
    2. 验草稿:不存在 404、已处理 409、内容不齐 400,此时零副作用;
    3. 意图审计(fail-closed):写不进 → 503,没有发布;
    4. 发布:失败时知识库没有改动,草稿仍待审;
    5. 草稿改记为已批准:失败则如实说「已发布、状态没更新」,重试不会重复发布;
    6. 结果审计 fail-open 但可见:响应里的 audit_recorded 为 false 表示缺这一条。
    """
    user_id = int(identity.get("user_id") or 0)
    async with _draft_lock(draft_id):
        draft = await _load_draft(draft_id)
        _require_pending(draft)
        try:
            publish.check_publishable(draft)
        except publish.DraftNotPublishableError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            await _write_draft_audit(
                user_id,
                draft,
                action=_draft_audit_action("approve_kb_draft_intent", draft),
                decision="approve",
                result="草稿批准受理(先审计后发布)",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="审计服务不可用,草稿未发布,请稍后重试"
            ) from exc
        try:
            source_id = await publish.publish_draft(draft, actor=f"user:{user_id}")
        except Exception as exc:  # noqa: BLE001 — 统一映射
            raise _map_publish_error(exc) from exc
        published_but_unmarked = HTTPException(
            status_code=500,
            detail=(
                f"内容已写入知识库(条目 {source_id}),但草稿状态更新失败;"
                "请重试批准,重试不会重复发布"
            ),
        )
        try:
            changed = await asyncio.to_thread(
                kb_ops_state.decide_draft,
                draft_id,
                status=kb_ops_state.DRAFT_APPROVED,
                decided_by=user_id,
                published_source_id=source_id,
            )
        except Exception as exc:
            raise published_but_unmarked from exc
        if not changed:
            raise published_but_unmarked

    audit_recorded = True
    try:
        await _write_draft_audit(
            user_id,
            draft,
            action=_draft_audit_action("approve_kb_draft", draft, source_id=source_id),
            decision="approve",
            result=f"草稿已发布到知识库条目 {source_id}",
        )
    except Exception:  # noqa: BLE001
        # 内容已经发布、无法撤回,不能因为结果审计失败而报失败;但缺口必须看得见
        audit_recorded = False
        logger.warning(
            "kb_ops 草稿发布结果审计写入失败 draft_id=%s source_id=%s——已发布,审计缺口需人工补记",
            draft_id,
            source_id,
            exc_info=True,
        )
    return {
        "draft_id": draft_id,
        "status": kb_ops_state.DRAFT_APPROVED,
        "published_source_id": source_id,
        "audit_recorded": audit_recorded,
    }


@router.post("/admin/kb/ops/drafts/{draft_id}/reject")
async def reject_kb_ops_draft(
    draft_id: int,
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
) -> dict[str, Any]:
    """驳回草稿:不发布,草稿归档。先落审计,写不进就不改状态(503)。"""
    user_id = int(identity.get("user_id") or 0)
    async with _draft_lock(draft_id):
        draft = await _load_draft(draft_id)
        _require_pending(draft)
        try:
            await _write_draft_audit(
                user_id,
                draft,
                action=_draft_audit_action("reject_kb_draft", draft),
                decision="reject",
                result="草稿被驳回,未发布",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="审计服务不可用,驳回未执行,请稍后重试"
            ) from exc
        try:
            changed = await asyncio.to_thread(
                kb_ops_state.decide_draft,
                draft_id,
                status=kb_ops_state.DRAFT_REJECTED,
                decided_by=user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("kb_ops 草稿驳回失败 draft_id=%s", draft_id, exc_info=True)
            raise HTTPException(status_code=500, detail="驳回失败,请稍后重试") from exc
    if not changed:
        raise HTTPException(status_code=409, detail="草稿已被处理")
    return {"draft_id": draft_id, "status": kb_ops_state.DRAFT_REJECTED}


# ── 忠实度抽样 ──────────────────────────────────────────


class FaithfulnessRunBody(BaseModel):
    """触发一次忠实度抽样:往回看多少天的对话,抽多少轮。"""

    days: int = Field(default=7, ge=1, le=30)
    sample_size: int = Field(default=30, ge=1, le=faithfulness_runner.MAX_SAMPLE_SIZE)


@router.post("/admin/kb/ops/faithfulness/runs", status_code=202)
async def start_faithfulness_run(
    identity: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    body: FaithfulnessRunBody | None = None,
) -> dict[str, Any]:
    """启动一次忠实度抽样(后台执行),返回 run_id。没在配置里打开或已有抽样在跑 → 409。"""
    body = body or FaithfulnessRunBody()
    user_id = int(identity.get("user_id") or 0)
    try:
        run_id = await faithfulness_runner.start_run(
            days=body.days, sample_size=body.sample_size, triggered_by=user_id
        )
    except (
        faithfulness_runner.FaithfulnessDisabledError,
        kb_ops_runner.RunInProgressError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 原始异常可能带连接串,只进日志
        logger.warning("kb_faithfulness 启动失败", exc_info=True)
        raise HTTPException(status_code=500, detail="启动抽样失败,请稍后重试") from exc
    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=f"kbfaith:{run_id}",
            acting_user_id=user_id,
            channel="kb_ops",
            agent="kb-faithfulness-runner",
            action={
                "op": "run_kb_faithfulness_sampling",
                "run_id": run_id,
                "days": body.days,
                "sample_size": body.sample_size,
            },
            decision=f"u{user_id}:run",
            result="已启动忠实度抽样",
        )
    except Exception:  # noqa: BLE001
        # 抽样已经在跑,而且它只读对话与知识库、只写自己的表:审计缺一条「谁触发的」
        # 不该让请求失败,但必须在日志里看得见(触发人另记在运行行上)
        logger.warning("kb_faithfulness 触发审计写入失败 run_id=%s", run_id, exc_info=True)
    return {"run_id": run_id, "status": "running"}


@router.get("/admin/kb/ops/faithfulness/runs")
async def list_faithfulness_runs(
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    limit: int = 20,
) -> dict[str, Any]:
    """最近的抽样运行,新的在前。忠实度与引用核验的数字都在每一项的 stats 里。"""
    limit = max(1, min(limit, 50))
    try:
        runs = await asyncio.to_thread(faithfulness_state.list_runs, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_faithfulness 运行列表查询失败", exc_info=True)
        raise HTTPException(status_code=500, detail="查询失败,请稍后重试") from exc
    return {"items": runs, "total": len(runs)}


@router.get("/admin/kb/ops/faithfulness/samples")
async def list_faithfulness_samples(
    _: Annotated[ResolvedIdentity, Depends(_require_kb_manage)],
    run_id: str | None = None,
    problems_only: bool = False,
) -> dict[str, Any]:
    """一次抽样的样本:那一轮的问题与回复、逐条说法的结论。有问题的排前面。

    run_id 缺省取最近一次成功的运行;一次都没成功过时返回空列表(run 为 null)。
    problems_only=true 只看有无依据或矛盾说法的样本。所在会话已删除的样本不再出现。
    """
    try:
        if run_id is None:
            run = await asyncio.to_thread(faithfulness_state.latest_succeeded_run)
        else:
            run = await asyncio.to_thread(faithfulness_state.get_run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="运行不存在")
        if run is None:
            return {"run": None, "items": [], "total": 0}
        samples = await asyncio.to_thread(faithfulness_state.list_samples, run["run_id"])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("kb_faithfulness 样本查询失败", exc_info=True)
        raise HTTPException(status_code=500, detail="查询失败,请稍后重试") from exc
    if problems_only:
        samples = [s for s in samples if s["unsupported"] or s["contradicted"]]
    items = [
        {
            **sample,
            # 落库时已脱敏;这里再过一遍,规则表后来新增的模式也能生效
            "user_message": mask_pii(str(sample["user_message"])),
            "reply_summary": mask_pii(str(sample["reply_summary"])),
        }
        for sample in samples
    ]
    return {"run": run, "items": items, "total": len(items)}
