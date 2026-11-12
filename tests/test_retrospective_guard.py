"""小格子抑制(retrospective/guard.py)单测:纯函数,直接喂原始聚合。

要守住的是「能落到个人头上的小计数进不了快照」:直接的小计数、能从比率反推的
小计数、能用合计减出来的小计数、以及分布键里夹带的文字。
"""

import pytest

from official_agent.retrospective.guard import (
    OTHER_KEY,
    SIDE_HIGH,
    SIDE_LOW,
    SMALL_CELL_MIN,
    UNIT_MS,
    UNIT_RATIO,
    RawBreakdown,
    RawCount,
    RawRatio,
    RawRatioBreakdown,
    RawStat,
    suppress,
)


def _only(raw):
    values = suppress(raw)
    assert len(values) == 1
    return values[0]


def _by_key(raw) -> dict:
    return {v.key: v for v in suppress(raw)}


# ── 计数 ────────────────────────────────────────────────


@pytest.mark.parametrize("small", [0, 1, SMALL_CELL_MIN - 1])
def test_count_below_threshold_is_suppressed(small: int) -> None:
    v = _only(RawCount("screening.jobs", small))
    assert v.suppressed is True
    assert v.value is None
    assert v.n is None  # 样本量就是这个小计数本身,同样不能给
    assert v.side == SIDE_LOW  # 展示为「<5」


def test_count_at_threshold_is_published() -> None:
    v = _only(RawCount("screening.jobs", SMALL_CELL_MIN))
    assert (v.value, v.n, v.suppressed) == (SMALL_CELL_MIN, SMALL_CELL_MIN, False)
    assert v.id == v.metric == "screening.jobs"


# ── 比率 ────────────────────────────────────────────────


def test_ratio_is_published_with_denominator_as_n() -> None:
    v = _only(RawRatio("kb.not_found_rate", 30, 120))
    assert (v.value, v.n, v.unit, v.suppressed) == (0.25, 120, UNIT_RATIO, False)


def test_ratio_with_small_numerator_is_suppressed() -> None:
    """30 人里 2 人:只看分母会放行,而 6.7% × 30 能反推出恰好 2 人。"""
    v = _only(RawRatio("screening.hard_zero_rate", 2, 30))
    assert v.suppressed is True and v.value is None
    assert v.n == 30  # 分母够大,可以给;分子仍然推不出来
    assert v.side == SIDE_LOW


def test_ratio_with_small_complement_is_suppressed() -> None:
    """198/200:比率本身很大,但它等于说「只有 2 个不是」。"""
    v = _only(RawRatio("screening.hard_zero_rate", 198, 200))
    assert v.suppressed is True and v.value is None
    assert v.side == SIDE_HIGH


def test_suppressed_ratio_says_which_end_it_is_near() -> None:
    """错误率被抑制时,读的人得分得清是「几乎没错」还是「几乎全错」。"""
    healthy = _only(RawRatio("conversation.error_rate", 0, 4000))
    broken = _only(RawRatio("conversation.error_rate", 3999, 4000))
    assert (healthy.side, broken.side) == (SIDE_LOW, SIDE_HIGH)
    assert healthy.value is None and broken.value is None


def test_ratio_with_small_denominator_hides_n_too() -> None:
    v = _only(RawRatio("screening.hard_zero_rate", 2, 4))
    assert v.suppressed is True and v.n is None
    assert v.side is None  # 样本本身不足,谈不上哪一头


def test_ratio_with_zero_denominator_does_not_divide() -> None:
    v = _only(RawRatio("kb.not_found_rate", 0, 0))
    assert v.suppressed is True and v.value is None and v.n is None


# ── 统计量 ──────────────────────────────────────────────


def test_stat_needs_enough_samples() -> None:
    few = _only(RawStat("conversation.duration_p50_ms", 1800, SMALL_CELL_MIN - 1, UNIT_MS))
    assert few.suppressed is True and few.value is None and few.n is None
    enough = _only(RawStat("conversation.duration_p50_ms", 1800, SMALL_CELL_MIN, UNIT_MS))
    assert (enough.value, enough.n, enough.unit) == (1800, SMALL_CELL_MIN, UNIT_MS)


def test_stat_without_value_is_not_published_as_zero() -> None:
    v = _only(RawStat("conversation.cache_hit_rate", None, 50, UNIT_RATIO))
    assert v.value is None and v.suppressed is True


# ── 分布 ────────────────────────────────────────────────


_DEPTS = frozenset({"技术部", "项目部", "媒体部"})


def test_breakdown_publishes_cells_with_stable_ids() -> None:
    cells = _by_key(
        RawBreakdown("screening.adopted_by_dept", {"技术部": 12, "媒体部": 7}, _DEPTS)
    )
    assert cells["技术部"].id == "screening.adopted_by_dept[技术部]"
    assert cells["技术部"].metric == "screening.adopted_by_dept"
    assert (cells["技术部"].value, cells["媒体部"].value) == (12, 7)


def test_breakdown_single_small_cell_takes_the_next_smallest_with_it() -> None:
    """只藏一格等于没藏:合计 − 其余各格就是它。所以再藏最小的一格。"""
    cells = _by_key(
        RawBreakdown(
            "screening.rejected_by_dept", {"技术部": 30, "项目部": 2, "媒体部": 9}, _DEPTS
        )
    )
    assert cells["项目部"].suppressed is True
    assert cells["媒体部"].suppressed is True  # 被连带抑制
    assert cells["技术部"].value == 30


def test_breakdown_two_small_cells_need_no_extra_suppression() -> None:
    cells = _by_key(
        RawBreakdown(
            "screening.rejected_by_dept", {"技术部": 30, "项目部": 2, "媒体部": 1}, _DEPTS
        )
    )
    assert cells["项目部"].suppressed and cells["媒体部"].suppressed
    assert cells["技术部"].value == 30


def test_breakdown_lone_small_cell_has_nothing_to_take() -> None:
    cells = _by_key(RawBreakdown("conversation.turns_by_channel", {"web": 3}))
    assert cells["web"].suppressed is True


# ── 分布里的 0 ──────────────────────────────────────────

_OUTCOMES = frozenset({"passed", "rejected"})


def test_breakdown_fixed_cell_is_emitted_even_when_absent() -> None:
    """判定结果的格子不出现 = 明说是 0。固定格缺席按 0 补,并照样被抑制。"""
    cells = _by_key(
        RawBreakdown("screening.adopted_by_dept", {"技术部": 60, "项目部": 20}, fixed_keys=_DEPTS)
    )
    assert set(cells) == {"技术部", "项目部", "媒体部"}
    assert cells["媒体部"].suppressed is True and cells["媒体部"].value is None
    # 只有媒体部一格被藏,合计减得出来——连带藏最小的项目部
    assert cells["项目部"].suppressed is True
    assert cells["技术部"].value == 60


def test_zero_and_small_in_one_department_cannot_be_combined() -> None:
    """媒体部 0 人被采纳、3 人被驳回:两格都看不出,推不出「看过的全被驳回」。"""
    adopted = _by_key(
        RawBreakdown("screening.adopted_by_dept", {"技术部": 60, "项目部": 20}, fixed_keys=_DEPTS)
    )
    rejected = _by_key(
        RawBreakdown(
            "screening.rejected_by_dept",
            {"技术部": 25, "项目部": 15, "媒体部": 3},
            fixed_keys=_DEPTS,
        )
    )
    assert adopted["媒体部"].suppressed and rejected["媒体部"].suppressed
    assert adopted["媒体部"].side == rejected["媒体部"].side == SIDE_LOW  # 0 与 3 长得一样


def test_breakdown_status_cell_with_nobody_in_it_is_left_out() -> None:
    """流程状态桶为 0 不出格子:「没有人还在待定」不指向谁,也不该连带藏掉通过数。"""
    cells = _by_key(
        RawBreakdown(
            "funnel.decisions",
            {"passed": 60, "rejected": 140, "pending": 0, "toTransfer": 0},
            fixed_keys=_OUTCOMES,
        )
    )
    assert set(cells) == {"passed", "rejected"}
    assert (cells["passed"].value, cells["rejected"].value) == (60, 140)


def test_breakdown_status_cell_with_a_few_people_is_still_protected() -> None:
    cells = _by_key(
        RawBreakdown(
            "funnel.decisions", {"passed": 60, "rejected": 140, "pending": 2}, fixed_keys=_OUTCOMES
        )
    )
    assert cells["pending"].suppressed is True
    assert cells["passed"].suppressed is True  # 连带抑制最小的一格
    assert cells["rejected"].value == 140


def test_breakdown_fixed_outcome_with_nobody_is_suppressed_not_dropped() -> None:
    """周期初还没人通过:passed 是 0,但格子在,且带走不通过那一格。"""
    cells = _by_key(
        RawBreakdown(
            "funnel.decisions", {"passed": 0, "rejected": 9, "pending": 40}, fixed_keys=_OUTCOMES
        )
    )
    assert cells["passed"].suppressed is True
    assert cells["rejected"].suppressed is True
    assert cells["pending"].value == 40


def test_breakdown_never_leaks_a_small_count() -> None:
    """不变量:任意组合下,出来的值要么被抑制,要么不小于阈值;被藏的格子不会只有一格。"""
    shapes = [
        {"a": 0},
        {"a": 4, "b": 5},
        {"a": 1, "b": 2, "c": 3},
        {"a": 100, "b": 4, "c": 4, "d": 50},
        {"a": 5, "b": 5, "c": 0},
        {"a": 50, "b": 0, "c": 7},
        {"c": 9, "d": 2},
    ]
    for shape in shapes:
        for fixed_keys in (frozenset(), frozenset({"a", "b"})):
            values = suppress(RawBreakdown("m", shape, fixed_keys=fixed_keys))
            for v in values:
                assert v.suppressed or v.value >= SMALL_CELL_MIN, (shape, fixed_keys)
            hidden = [v for v in values if v.suppressed]
            if hidden and len(values) > 1:
                assert len(hidden) >= 2, f"只藏一格可被合计减出:{shape} {fixed_keys}"


# ── 分布的键 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "13812345678",  # 手机号:形状是字母数字,靠 PII 规则拦
        "202311112222",  # 学号
        "zhang.san@example.com",  # 邮箱
        "张三",  # 中文键不在白名单里,分不清是部门还是人名
        "我想问一下技术部今年还招不招人",  # 一句原话
        "search knowledge",  # 带空格的自由文本
        "x" * 41,  # 超长
        "",
    ],
)
def test_breakdown_unsafe_key_is_folded_into_other(unsafe_key: str) -> None:
    cells = _by_key(
        RawBreakdown("conversation.tool_calls", {unsafe_key: 20, "search_knowledge": 40})
    )
    assert set(cells) == {OTHER_KEY, "search_knowledge"}
    assert cells[OTHER_KEY].value == 20
    if unsafe_key:
        assert all(unsafe_key not in v.id for v in cells.values())


def test_breakdown_chinese_key_passes_only_through_the_allowlist() -> None:
    cells = _by_key(RawBreakdown("screening.adopted_by_dept", {"技术部": 8, "张三": 6}, _DEPTS))
    assert set(cells) == {"技术部", OTHER_KEY}


def test_breakdown_unsafe_keys_are_merged_before_suppression() -> None:
    """两个不合规的键各 3 次:并成一格 6 次再判,而不是各自被抑制后丢掉。"""
    cells = _by_key(
        RawBreakdown("conversation.tool_calls", {"bad key": 3, "another bad": 3, "ok_tool": 9})
    )
    assert cells[OTHER_KEY].value == 6


# ── 分组比率 ────────────────────────────────────────────


def test_ratio_breakdown_judges_each_cell_on_its_own() -> None:
    cells = _by_key(
        RawRatioBreakdown("qbank.pick_rate_by_group", {"repo": (30, 120), "awards": (2, 40)})
    )
    assert (cells["repo"].value, cells["repo"].n) == (0.25, 120)
    assert cells["repo"].id == "qbank.pick_rate_by_group[repo]"
    assert cells["awards"].suppressed is True and cells["awards"].value is None
