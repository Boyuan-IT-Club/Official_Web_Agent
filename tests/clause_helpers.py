"""条款候选测试的公共构造:一个合成的已结束周期,人和 AI 在两头看法相反。"""

from __future__ import annotations

from typing import Any

POOL_SIZE = 15
RATIOS = {"top_ratio": 0.2, "bottom_ratio": 0.2, "min_pool": POOL_SIZE}


def card(resume_id: int, *, first_dept: str = "技术部", quote: str | None = None) -> dict[str, Any]:
    """一张合成的卡。AI 的分随简历号递增;简历号 ≥ 8 的「技术基础」判达成,其余判未达成。"""
    tech_met = resume_id >= 8
    tech_quote = quote if quote is not None else f"第 {resume_id} 份:用 Flask 写了小程序"
    return {
        "hard_zero": False,
        "intended": {"first": first_dept},
        "match_score": float(resume_id),
        "effort_score": 0.0,
        "match": [
            {
                "dept": "技术部",
                "items": [
                    {
                        "item": "技术基础",
                        "met": tech_met,
                        "quote": tech_quote if tech_met else "",
                        "reason": "有做出来的东西" if tech_met else "只列了技术名词",
                    }
                ],
            },
            {
                "dept": "媒体部",
                "items": [
                    {"item": "相关经历作品", "met": True, "quote": "做过海报", "reason": "有作品"}
                ],
            },
        ],
        "effort": {
            "items": [{"item": "表达成文", "met": True, "quote": "条理清楚", "reason": "成句成段"}]
        },
    }


def cycle_cards(**overrides: dict[str, Any]) -> list[dict[str, Any]]:
    """十五份简历的卡;overrides 按简历号替换其中几张。"""
    return [
        {"resume_id": rid, "card_version": 1, "card": overrides.get(str(rid), card(rid))}
        for rid in range(1, POOL_SIZE + 1)
    ]


def reversed_human_scores() -> dict[int, int]:
    """评审人给的分与 AI 的排序正好相反:AI 排最前的三份,人给了最低的三个分。"""
    return {rid: (POOL_SIZE + 1 - rid) * 6 for rid in range(1, POOL_SIZE + 1)}
