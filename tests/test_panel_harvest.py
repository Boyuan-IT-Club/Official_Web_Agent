"""从往届评分卡收割校准案例的规则单测:人机一致怎么算、卡里哪些判定能成为案例。纯函数。"""

from official_agent.evaluation.panel import harvest

RATIOS = {"top_ratio": 0.2, "bottom_ratio": 0.2}


def _entry(resume_id: int, ai: float, human: float, pool: str = "技术部") -> dict:
    return {"resume_id": resume_id, "pool": pool, "ai_score": ai, "human_score": human}


def test_position_band_depends_on_rank_only() -> None:
    pool = [float(v) for v in range(1, 11)]  # 十个人,各不相同

    assert harvest.position_band(10.0, pool, **RATIOS) == "top"
    assert harvest.position_band(9.0, pool, **RATIOS) == "top"  # 比它高的只有 1/10
    assert harvest.position_band(8.0, pool, **RATIOS) == "middle"
    assert harvest.position_band(3.0, pool, **RATIOS) == "middle"
    assert harvest.position_band(2.0, pool, **RATIOS) == "bottom"
    # 同样的位次,换一个量纲(人给的 0-100 分)结论不变
    assert harvest.position_band(90.0, [v * 10 for v in pool], **RATIOS) == "top"


def test_consistent_means_same_band_in_the_same_pool() -> None:
    """人给 0-100 分、AI 是两维 0-10 分,量纲不同:比的是各自在池里的位次带。"""
    entries = [_entry(i, ai=float(i), human=float(i * 5)) for i in range(1, 11)]
    entries[9] = _entry(10, ai=10.0, human=1.0)  # AI 排第一,人给了最低分
    entries[0] = _entry(1, ai=1.0, human=99.0)  # AI 排最后,人给了最高分

    consistent = harvest.consistent_resumes(entries, **RATIOS, min_pool=10)

    assert 10 not in consistent and 1 not in consistent
    assert {4, 5, 6, 7} <= consistent  # 中段两边看法一致


def test_small_pools_yield_nothing_and_pools_are_judged_separately() -> None:
    tech = [_entry(i, ai=float(i), human=float(i)) for i in range(1, 11)]
    media = [_entry(100 + i, ai=float(i), human=float(i), pool="媒体部") for i in range(1, 4)]

    consistent = harvest.consistent_resumes(tech + media, **RATIOS, min_pool=10)

    assert consistent == set(range(1, 11))  # 媒体部只有三个人,位次带没有意义,整池不取


def _card(**over) -> dict:
    card = {
        "hard_zero": False,
        "match_score": 7.0,
        "effort_score": 6.0,
        "match": [
            {
                "dept": "技术部",
                "items": [
                    {"item": "技术基础", "met": True, "quote": "写了小程序", "reason": "有产出"},
                    {"item": "分享意愿", "met": False, "quote": "", "reason": "没提到分享"},
                    {
                        "item": "探索内驱力",
                        "met": False,
                        "quote": "对编程感兴趣",
                        "reason": "只有兴趣",
                    },
                ],
            }
        ],
        "effort": {
            "items": [
                {"item": "表达成文", "met": True, "quote": "条理清楚的一段", "reason": "成文"}
            ]
        },
    }
    card.update(over)
    return card


def test_cards_the_ai_was_unsure_about_are_not_harvested() -> None:
    assert harvest.harvestable(_card())
    assert not harvest.harvestable(_card(hard_zero=True))
    assert not harvest.harvestable(_card(match_score=None))  # 没填志愿或旧卡
    assert not harvest.harvestable(_card(panel={"confidence": "low"}))
    assert not harvest.harvestable(_card(panel={"injection": {"detected": True}}))
    assert harvest.harvestable(_card(panel={"confidence": "medium", "injection": {}}))


def test_candidates_are_met_items_and_near_misses_with_a_quote() -> None:
    """没有引文的未达成判定只说明「没写到」,示范不了标尺,不取。"""
    cases = harvest.candidate_cases(_card())

    assert [(c["checklist"], c["item"], c["met"]) for c in cases] == [
        ("技术部", "技术基础", True),
        ("技术部", "探索内驱力", False),  # 近失:有这句话,但仍不算达成
        ("认真程度", "表达成文", True),
    ]


def test_disputed_and_struck_out_items_are_not_candidates() -> None:
    panel = {
        "disputes": [{"checklist": "技术部", "item": "技术基础"}],
        "overrides": [{"checklist": "认真程度", "item": "表达成文"}],
    }

    cases = harvest.candidate_cases(_card(panel=panel))

    assert [(c["checklist"], c["item"]) for c in cases] == [("技术部", "探索内驱力")]
