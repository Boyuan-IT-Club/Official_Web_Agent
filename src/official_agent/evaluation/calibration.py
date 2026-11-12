"""校准案例的编排:算向量、读写案例库。规则在 panel/anchors.py 与 panel/harvest.py,这里只管 IO。

三件事:
- 评分前给一份简历取判例(retrieve_for_resume):失败不抛,评委不带案例照常判;
- 录入种子判例(add_seed_case):部长手写的判例,冷启动靠它;
- 从已结束的周期收割人机一致的卡(harvest_cycle):需要至少一个完整周期的人工采纳数据。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from official_agent.config import get_settings
from official_agent.evaluation.grading import GradingPolicy
from official_agent.evaluation.panel import anchors, harvest
from official_agent.kb.embedding import embed_texts
from official_agent.state.evaluation import calibration_store

#: 一份简历最多拿多少个片段(栏)去算向量;超出的栏不参与检索
_MAX_FRAGMENTS = 12
#: 收割时一批送去算向量的案例数
_EMBED_BATCH = 32


async def retrieve_for_resume(
    fields: list[dict[str, Any]], *, resume_id: int, cycle_id: int, watermark: int
) -> anchors.Anchors:
    """给一份简历取这次评分能用的判例。fields 是脱敏后的简历各栏(同评分入口)。

    检索是纯代码:在本周期冻结的案例集里,按判定项取与简历片段最相似的判例。任何失败
    (嵌入服务不可用、库读不到)都不抛——判例是增强不是依赖,返回 status=unavailable,
    评委不带案例照常判。
    """
    try:
        cases = await asyncio.to_thread(
            calibration_store.load_cases_for_retrieval, get_settings().embed_model
        )
        usable = anchors.eligible_cases(
            cases, watermark=watermark, cycle_id=cycle_id, resume_id=resume_id
        )
        fragments = [str(f.get("value", "")) for f in fields if str(f.get("value", "")).strip()]
        if not usable or not fragments:
            return anchors.Anchors(set=watermark, status="ok", by_item={})
        vectors = await embed_texts(fragments[:_MAX_FRAGMENTS])
        selected = anchors.select_anchors(usable, vectors)
    except Exception as exc:  # noqa: BLE001 — 判例取不到不挡评分
        logging.getLogger(__name__).warning(
            "eval_calibration_unavailable resume_id=%s error_class=%s",
            resume_id,
            type(exc).__name__,
            exc_info=True,
        )
        return anchors.Anchors(set=watermark, status="unavailable", by_item={})
    return anchors.Anchors(set=watermark, status="ok", by_item=selected)


async def add_seed_case(
    *, checklist: str, item: str, met: bool, quote: str, reason: str, created_by: int | None
) -> dict[str, Any]:
    """录入一条种子判例:入库筛查 → 算向量 → 落库。返回入库后的案例(不带向量)。

    Raises:
        anchors.CaseRejectedError: 没通过入库筛查(判定项不存在、缺内容、含指令样文本)。
        official_agent.kb.embedding.EmbeddingError: 嵌入服务未配置或调用失败,案例未入库。
    """
    case = anchors.screen_case(
        checklist=checklist, item=item, met=met, quote=quote, reason=reason
    )
    (vector,) = await embed_texts([anchors.case_text(case)])
    case_id = await asyncio.to_thread(
        calibration_store.add_case,
        case,
        origin="seed",
        embedding=vector,
        embed_model=get_settings().embed_model,
        created_by=created_by,
    )
    return {"case_id": case_id, "origin": "seed", **case}


async def harvest_cycle(
    cycle_id: int, *, policy: GradingPolicy, created_by: int | None
) -> dict[str, int]:
    """把一个已结束周期里人机一致的卡收割成校准案例,返回各步的计数。

    调用方负责确认周期已经结束。重复收割同一个周期是幂等的:同一张卡的同一个判定项
    只入库一次。新入库的案例只对之后才开跑的周期生效。

    Raises:
        official_agent.kb.embedding.EmbeddingError: 嵌入服务不可用。已经入库的批次保留,
            重新收割会补上剩下的。
    """
    cards = await asyncio.to_thread(calibration_store.list_adopted_cards, cycle_id)
    human_scores = await asyncio.to_thread(calibration_store.adopted_human_scores, cycle_id)
    entries = harvest.comparable_entries(cards, human_scores)
    consistent = harvest.consistent_resumes(
        entries,
        top_ratio=policy.top_ratio,
        bottom_ratio=policy.bottom_ratio,
        min_pool=policy.min_pool,
    )

    screened: list[tuple[int, dict[str, Any]]] = []
    rejected = 0
    for entry in entries:
        if entry["resume_id"] not in consistent:
            continue
        for raw in harvest.candidate_cases(entry["card"]):
            try:
                screened.append((entry["resume_id"], anchors.screen_case(**raw)))
            except anchors.CaseRejectedError:
                rejected += 1

    added = 0
    embed_model = get_settings().embed_model
    for start in range(0, len(screened), _EMBED_BATCH):
        batch = screened[start : start + _EMBED_BATCH]
        vectors = await embed_texts([anchors.case_text(case) for _, case in batch])
        for (resume_id, case), vector in zip(batch, vectors, strict=True):
            case_id = await asyncio.to_thread(
                calibration_store.add_case,
                case,
                origin="adopted_card",
                embedding=vector,
                embed_model=embed_model,
                source_cycle_id=cycle_id,
                source_resume_id=resume_id,
                created_by=created_by,
            )
            added += case_id is not None
    return {
        "adopted_cards": len(cards),
        "comparable_cards": len(entries),
        "consistent_cards": len(consistent),
        "cases_added": added,
        "cases_rejected": rejected,
    }
