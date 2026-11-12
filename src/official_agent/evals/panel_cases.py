"""合议类 eval 数据集的公共解析:把用例里的简写展开成评分子图内部用的数据形状。

数据集里只写「评委判了哪几项达成、引文是什么」,其余条目一律视为未达成——逐项写满
二十一项的用例没法读。
"""

from __future__ import annotations

from typing import Any

from official_agent.evaluation.panel.shapes import ItemKey, Judgement
from official_agent.evaluation.schema import (
    CHECKLISTS,
    DEPARTMENTS,
    DEPT_MATCH_ITEMS,
    EFFORT_CHECKLIST,
    EFFORT_ITEMS,
    checklist_item_names,
    item_names,
)


def item_key(raw: dict[str, Any], *, case_id: str) -> ItemKey:
    """用例里的 {checklist, item} → 条目定位;清单或判定项写错直接抛,不静默放过。"""
    checklist, item = str(raw.get("checklist", "")), str(raw.get("item", ""))
    if checklist not in CHECKLISTS or item not in checklist_item_names(checklist):
        raise ValueError(f"用例 {case_id}:没有「{checklist}·{item}」这个判定项")
    return checklist, item


def judgement_of(case: dict[str, Any]) -> Judgement:
    """用例 → 评委判定。case["met"] 列出达成的条目与引文;attitude 缺省 sincere。"""
    case_id = str(case.get("id", "?"))
    quotes = {item_key(raw, case_id=case_id): str(raw["quote"]) for raw in case.get("met") or []}

    def verdict(checklist: str, name: str) -> dict[str, Any]:
        quote = quotes.get((checklist, name), "")
        return {
            "item": name,
            "met": bool(quote),
            "quote": quote,
            "reason": "数据集给定的判定" if quote else "数据集里未列为达成",
        }

    return Judgement(
        match=[
            {
                "dept": dept,
                "items": [verdict(dept, name) for name in item_names(DEPT_MATCH_ITEMS[dept])],
            }
            for dept in DEPARTMENTS
        ],
        effort=[verdict(EFFORT_CHECKLIST, name) for name in item_names(EFFORT_ITEMS)],
        summary=str(case.get("summary") or "数据集用例"),
        attitude={"verdict": str(case.get("attitude") or "sincere"), "reason": "数据集给定"},
    )


def resume_fields_of(case: dict[str, Any]) -> list[dict[str, str]]:
    """用例里的简历各栏 → 评分入口要的字段列表;文本先脱敏,与线上评分入口同一条纪律。"""
    from official_agent.security.pii import mask_pii

    return [
        {
            "field_key": f"f{j}",
            "title": mask_pii(str(f.get("title", ""))),
            "value": mask_pii(str(f.get("value", ""))),
        }
        for j, f in enumerate(case.get("fields") or [])
    ]
