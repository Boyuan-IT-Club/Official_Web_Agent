"""数字落地守卫(retrospective/grounding.py)单测:纯函数,直接喂句子。

要守住的是「报告里不出现快照之外的数字」:编出来的数、算错的约数、换了写法的
小计数(中文数字、模糊量词)都要被抓出来,而照抄指标值的合规写法不能误伤。
"""

import pytest

from official_agent.retrospective import grounding

SNAPSHOT = {
    "cycle_id": 12,
    "baseline": {"cycle_id": 11},
    "window": {"start": "2026-09-01T00:00:00+08:00", "end": "2026-12-01T00:00:00+08:00"},
    "params": {"kb_min_score": 0.45},
    "metrics": [
        {"id": "funnel.resumes_total"},
        {"id": "funnel.assigned_by_dept[3]"},
        {"id": "kb.not_found_rate"},
    ],
    "catalog": [{"id": "funnel.resumes_total"}, {"id": "funnel.assigned_by_dept"}],
}
CONTEXT = grounding.context_for(SNAPSHOT)

# 这句话引用的指标:投递数(带基线)、未命中率(带基线)、输入 token、耗时
CITED = [
    {"unit": "count", "value": 236, "n": 236, "prev": 200, "delta": 36, "delta_ratio": 0.18},
    {"unit": "ratio", "value": 0.2534, "n": 2500, "prev": 0.2, "delta": 0.0534},
    {"unit": "tokens", "value": 9_634_112, "n": 3800},
    {"unit": "ms", "value": 20480, "n": 3900},
]
ALLOWED = grounding.allowed_from(CITED)


def _bad_tokens(text: str, allowed=ALLOWED) -> list[str]:
    return [t for v in grounding.check_text(text, allowed, CONTEXT) for t in v.tokens]


# ── 合规写法放行 ────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "本周期共投递 236 份,比基线的 200 份多 36 份,增加 18%。",
        "未命中率 25.34%,共 2500 次检索。",
        "未命中率 25.3%,也就是 0.25。",  # 按写出的位数四舍五入
        "未命中率约 25%。",
        "未命中率较基线上升 5.3 个百分点。",
        "未命中率约 2.5 成。",
        "输入 9,634,112 token,约 963 万。",  # 千分位与「万」的约数
        "输入约 9.6M token,共 3800 轮有用量记录。",
        "耗时的第 99 百分位是 20,480 毫秒,P99 偏高。",  # 分位数标记不是报告陈述的量
        "周期 12 相比周期 11:投递 236 份。",  # 周期编号
        "2026 年 9 月 1 日到 11 月 30 日之间投递 236 份。",  # 时间窗的首尾两天
        "2026-09-01 起投递 236 份。",
        "funnel.resumes_total 是 236,funnel.assigned_by_dept[3] 没有异常。",  # 指标 id 里的数字
        "1. 投递 236 份。\n2. 检索 2500 次。",  # 列表序号
        "有一格不足 5,另一格少于 5 人,还有一格 <5。",  # 抑制用语
        "向量门槛是 0.45。",  # 采集参数
        "投递量平稳,没有明显变化。",  # 不带数字
    ],
)
def test_faithful_wording_passes(text: str) -> None:
    assert _bad_tokens(text) == []


# ── 对不上的被抓出 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("本周期共投递 240 份。", ["240"]),  # 编的
        ("投递约 230 份。", ["230"]),  # 没写成约数的形式,就得一字不差
        ("未命中率 25.6%。", ["25.6%"]),  # 四舍五入不对
        ("未命中率 30%。", ["30%"]),
        ("未命中率上升了 5.3%。", ["5.3%"]),  # 百分点差不能写成百分比
        ("输入约 960 万 token。", ["960 万"]),  # 9,634,112 四舍五入是 963 万
        ("有 5 人被驳回。", ["5"]),  # 阈值 5 只在「不足 5」这类说法里放行
        ("比率是 0.3。", ["0.3"]),  # 小数写法至少两位
        ("共 0 条错误。", ["0"]),  # 快照里没有 0(被抑制或不出格子)
        ("10 月 15 日是投递高峰。", ["10", "15"]),  # 快照里没有的日期
        ("周期 13 表现更好。", ["13"]),  # 不是本周期也不是基线周期
        ("投递 236 份,其中 37 份来自技术部。", ["37"]),  # 一句里有一个对不上就整句算
    ],
)
def test_invented_or_miscalculated_numbers_are_caught(text: str, tokens: list[str]) -> None:
    assert _bad_tokens(text) == tokens


def test_number_must_come_from_the_cited_metrics_not_anywhere_in_the_snapshot() -> None:
    """2500 是检索次数。只引用了投递数的一句话里出现 2500,不能因为快照里有就放行。"""
    only_resumes = grounding.allowed_from([CITED[0]])
    assert _bad_tokens("共有 2500 人投递。", only_resumes) == ["2500"]
    assert _bad_tokens("共检索 2500 次。") == []


def test_percentage_points_are_only_matched_against_a_change_in_a_ratio() -> None:
    """「25.3 个百分点」是在说变化量:不能拿比率本身(25.34%)去对。"""
    assert _bad_tokens("未命中率上升 25.3 个百分点。") == ["25.3 个百分点"]
    assert _bad_tokens("未命中率上升 5.34 个百分点。") == []


def test_plain_number_is_not_matched_against_a_ratio_or_the_reverse() -> None:
    ratio_only = grounding.allowed_from([{"unit": "ratio", "value": 0.25, "n": 80}])
    assert _bad_tokens("共 25 人。", ratio_only) == ["25"]  # 25% 不等于 25 个
    count_only = grounding.allowed_from([{"unit": "count", "value": 25, "n": 25}])
    assert _bad_tokens("占 25%。", count_only) == ["25%"]


# ── 守卫看不懂的量 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("大约三成的检索没有命中。", "三成"),
        ("错误翻了一倍。", "一倍"),
        ("错误量翻倍。", "翻倍"),
        ("三分之一的简历被驳回。", "三分之一"),
        ("过半的人选了技术部。", "过半"),
        ("一半的检索没命中。", "一半"),
        ("项目部只有两人被驳回。", "两人"),  # 小格子换成中文数字照样是点名
        ("有三名候选人没有分配部门。", "三名"),
        ("十几个人没来面试。", "十几个人"),
    ],
)
def test_fuzzy_quantifiers_and_chinese_headcounts_are_refused(text: str, token: str) -> None:
    assert token in _bad_tokens(text)


@pytest.mark.parametrize(
    "text",
    [
        "每一位候选人都收到了答复。",
        "评审成员反馈良好,知识库成为主要入口。",
        "这是第一名的部门。",
        "半成品的题库不应发布。",
        "多数成员认为流程顺畅。",
    ],
)
def test_ordinary_words_are_not_mistaken_for_quantities(text: str) -> None:
    assert _bad_tokens(text) == []


# ── 全角写法 ────────────────────────────────────────────


def test_fullwidth_digits_and_punctuation_are_checked_the_same_way() -> None:
    """模型写中文时会用全角的数字、百分号、分号。"""
    faithful = "未命中率 ２５.３４％；共检索 ２５００ 次！"
    assert _bad_tokens(faithful) == []
    invented = "未命中率 ３０％；"
    assert _bad_tokens(invented) == ["30%"]


# ── 逐句处理 ────────────────────────────────────────────


def test_only_the_offending_sentence_is_reported_and_removed() -> None:
    text = "本周期共投递 236 份。其中 99 份来自外校;未命中率 25.3%。"
    violations = grounding.check_text(text, ALLOWED, CONTEXT)
    assert [(v.sentence, v.tokens) for v in violations] == [("其中 99 份来自外校;", ("99",))]
    assert grounding.remove_sentences(text, violations) == "本周期共投递 236 份。未命中率 25.3%。"


def test_removing_every_sentence_leaves_nothing() -> None:
    text = "共 99 份。"
    violations = grounding.check_text(text, ALLOWED, CONTEXT)
    assert grounding.remove_sentences(text, violations) == ""


# ── 放行项与千分位 ──────────────────────────────────────


def test_allowed_numbers_cover_value_sample_baseline_and_change() -> None:
    allowed = {(a.value, a.kind) for a in grounding.allowed_from([CITED[1]])}
    assert allowed == {
        (0.2534, grounding.KIND_RATIO),
        (0.2, grounding.KIND_RATIO),
        (2500.0, grounding.KIND_PLAIN),
        (0.0534, grounding.KIND_POINTS),
    }


def test_suppressed_metric_contributes_only_its_visible_sample_size() -> None:
    suppressed = {"unit": "ratio", "value": None, "n": 4000, "suppressed": True, "side": "low"}
    allowed = grounding.allowed_from([suppressed])
    assert [(a.value, a.kind) for a in allowed] == [(4000.0, grounding.KIND_PLAIN)]
    assert _bad_tokens("错误不足 5,共 4000 轮。", allowed) == []
    assert _bad_tokens("错误 3 次,共 4000 轮。", allowed) == ["3"]


def test_context_without_window_or_baseline() -> None:
    context = grounding.context_for({"cycle_id": 7, "metrics": [], "catalog": []})

    def bad(text: str) -> list[str]:
        return [t for v in grounding.check_text(text, ALLOWED, context) for t in v.tokens]

    assert context.always == ()
    assert bad("周期 7 投递 236 份。") == []
    assert bad("周期 12 投递 236 份。") == ["12"]  # 别的周期编号不放行
    assert bad("9 月 1 日投递 236 份。") == ["9", "1"]  # 没有时间窗就没有可放行的日期


def test_separate_thousands_changes_the_writing_not_the_number() -> None:
    text = "输入 9634112 token,P99 是 20480 毫秒;比率 25.34%,周期 12,共 236 份,约 963 万。"
    assert grounding.separate_thousands(text) == (
        "输入 9,634,112 token,P99 是 20,480 毫秒;比率 25.34%,周期 12,共 236 份,约 963 万。"
    )
    # 整理之后仍然对得上
    assert _bad_tokens(grounding.separate_thousands("输入 9634112 token。")) == []


# ── 清单项的文字(主题名)────────────────────────────────

_TOPIC_SNAPSHOT = {
    **SNAPSHOT,
    "metrics": [
        *SNAPSHOT["metrics"],
        {"id": "kb.top_topics[t01]", "label": "2026 秋招三人组队规则"},
        {"id": "kb.top_topics[t02]", "label": "社团有 300 多人吗"},
    ],
}
_TOPIC_CONTEXT = grounding.context_for(_TOPIC_SNAPSHOT)
_TOPIC_ALLOWED = grounding.allowed_from([{"unit": "count", "value": 120, "n": 120}])


def _topic_tokens(text: str) -> list[str]:
    violations = grounding.check_text(text, _TOPIC_ALLOWED, _TOPIC_CONTEXT)
    return [t for v in violations for t in v.tokens]


def test_numbers_inside_a_quoted_topic_name_are_not_claims() -> None:
    assert _topic_tokens("主题「2026 秋招三人组队规则」被问了 120 次。") == []
    assert _topic_tokens("「社团有 300 多人吗」被问了 120 次。") == []


def test_a_reworded_topic_name_gets_no_such_pass() -> None:
    """只认一字不差的照抄:改写过的名字里的数,照常要在引用的指标里对得上。"""
    assert _topic_tokens("社团有 300 人这件事被问了 120 次。") == ["300"]
    assert _topic_tokens("三人组队的规则被问了 120 次。") == ["三人"]


def test_quoting_a_topic_name_does_not_excuse_other_numbers_in_the_sentence() -> None:
    assert _topic_tokens("主题「2026 秋招三人组队规则」被问了 450 次。") == ["450"]
