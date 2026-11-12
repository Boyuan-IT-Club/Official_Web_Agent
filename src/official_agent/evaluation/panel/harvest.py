"""从往届的评分卡里收割校准案例的确定性规则:哪些卡算「人机一致」、卡里哪些判定能成为案例。纯函数。

只有人机一致的卡才能成为案例:人和 AI 看法不同的卡,不知道是谁错了,不能拿来当标尺。
人的信号是采纳时打的 0-100 分,AI 的信号是两维 0-10 分,量纲不同,所以比的是**池内
位次**:各自在同一个候选池里落在头部、中部还是尾部。
"""

from __future__ import annotations

from typing import Any

from official_agent.evaluation.panel.shapes import ItemKey
from official_agent.evaluation.schema import EFFORT_CHECKLIST


def position_band(value: float, pool: list[float], *, top_ratio: float, bottom_ratio: float) -> str:
    """一个值在池子里的位次带:top / middle / bottom。pool 应包含 value 自己。

    只看位次,不看绝对值:头部是「比它高的不到 top_ratio」,尾部是「不高于它的不超过
    bottom_ratio」。与等级划分用的是同一对比例,但不带分数锚点——那些锚点是给 0-10 分
    定的,套不到人给的 0-100 分上。
    """
    above = sum(1 for other in pool if other > value)
    if above / len(pool) < top_ratio:
        return "top"
    if (len(pool) - above) / len(pool) <= bottom_ratio:
        return "bottom"
    return "middle"


def consistent_resumes(
    entries: list[dict[str, Any]], *, top_ratio: float, bottom_ratio: float, min_pool: int
) -> set[int]:
    """人机一致的简历号:AI 的位次带与人给的分的位次带相同。

    entries 每项:resume_id、pool(候选池名)、ai_score、human_score。各池分开比。
    不足 min_pool 人的池子整池不取:人数太少时位次带没有意义,宁可不要这批案例。
    """
    pools: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        pools.setdefault(entry["pool"], []).append(entry)
    consistent: set[int] = set()
    for members in pools.values():
        if len(members) < min_pool:
            continue
        ai_scores = [m["ai_score"] for m in members]
        human_scores = [m["human_score"] for m in members]
        for member in members:
            ratios = {"top_ratio": top_ratio, "bottom_ratio": bottom_ratio}
            ai_band = position_band(member["ai_score"], ai_scores, **ratios)
            human_band = position_band(member["human_score"], human_scores, **ratios)
            if ai_band == human_band:
                consistent.add(member["resume_id"])
    return consistent


def harvestable(card: dict[str, Any]) -> bool:
    """这张卡的判定能不能拿来当案例的来源。

    不要:命中绝对卡或无一项达成的卡(没有可示范的判定)、没有两维分数的旧卡、检出过
    提示注入的卡、AI 自己都拿不准(低置信)的卡。
    """
    if card.get("hard_zero") or card.get("match_score") is None:
        return False
    if card.get("effort_score") is None:
        return False
    panel = card.get("panel") or {}
    if (panel.get("injection") or {}).get("detected"):
        return False
    return panel.get("confidence") != "low"


def candidate_cases(card: dict[str, Any]) -> list[dict[str, Any]]:
    """一张卡里可以成为案例的判定(尚未经入库筛查)。

    取达成的判定,以及带了引文的未达成判定(「有这句话,但仍不算达成」)。没有引文的
    未达成判定不取:它只说明「没写到」,示范不了标尺。有过分歧、或被程序作废的条目
    不取——那几项恰恰是没有定论的。
    """
    panel = card.get("panel") or {}
    unsettled: set[ItemKey] = {
        (entry["checklist"], entry["item"])
        for entry in [*(panel.get("disputes") or []), *(panel.get("overrides") or [])]
    }
    located = [(m["dept"], item) for m in card.get("match") or [] for item in m.get("items") or []]
    located += [
        (EFFORT_CHECKLIST, item) for item in (card.get("effort") or {}).get("items") or []
    ]
    return [
        {
            "checklist": checklist,
            "item": item["item"],
            "met": bool(item["met"]),
            "quote": str(item.get("quote") or ""),
            "reason": str(item.get("reason") or ""),
        }
        for checklist, item in located
        if (checklist, item["item"]) not in unsettled and str(item.get("quote") or "").strip()
    ]
