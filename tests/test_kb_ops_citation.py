"""引文复核与查重的单测:纯函数。"""

import pytest

from official_agent.kb_ops.citation import normalize, quote_in_text, ungrounded_numbers
from official_agent.kb_ops.dedupe import KnownQuestion, most_similar, similar_refs

_TEXT = (
    "技术部面试分两轮:第一轮是三十分钟的技术面,第二轮是部长面。"
    "面试安排在周六下午,地点在信息楼 302。"
)


def test_normalize_keeps_only_letters_and_digits() -> None:
    assert normalize("信息楼 302,B座(东门)!ABC") == "信息楼302b座东门abc"


@pytest.mark.parametrize(
    "quote",
    [
        "面试安排在周六下午",
        "面试安排在周六下午, 地点在信息楼302",  # 空白、半角标点
        "技术部面试分两轮…第二轮是部长面",  # 省略号两侧各自命中且顺序一致
        "技术部面试分两轮...面试安排在周六下午",
    ],
)
def test_quotes_found_in_source(quote: str) -> None:
    assert quote_in_text(quote, _TEXT) is True


@pytest.mark.parametrize(
    "quote",
    [
        "面试安排在周日下午",  # 改了一个字
        "面试需要现场手写代码",  # 原文里没有
        "第二轮是部长面…技术部面试分两轮",  # 顺序反了
        "技术部面试分两轮…需要手写代码",  # 省略号后半段是编的
        "技术部",  # 太短,证明不了什么
        "技术部面试…部…",  # 有一段太短
        "",
        "…",
    ],
)
def test_quotes_not_found_in_source(quote: str) -> None:
    assert quote_in_text(quote, _TEXT) is False


def test_quote_longer_than_source_is_not_found() -> None:
    assert quote_in_text(_TEXT + "另外还要交报名费", _TEXT) is False


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("面试地点在信息楼 302。", []),
        ("面试地点在信息楼 \uff13\uff10\uff12。", []),  # 全角数字与半角是同一个数
        ("入社需要交 500 元。", ["500"]),
        ("第一轮技术面 30 分钟,地点在 302。", ["30"]),  # 原文写的是「三十分钟」
        ("报名费 500 元,押金 500 元,另收 20.5 元。", ["500", "20.5"]),
        ("没有数字的句子。", []),
    ],
)
def test_ungrounded_numbers(sentence: str, expected: list[str]) -> None:
    assert list(ungrounded_numbers(sentence, _TEXT)) == expected


# ── 查重 ────────────────────────────────────────────────

_KNOWN = [
    KnownQuestion(ref="kb_fee", text="加入社团需要交费吗", vector=[1.0, 0.0, 0.0]),
    KnownQuestion(ref="kb_interview", text="面试在哪里", vector=[0.0, 1.0, 0.0]),
    KnownQuestion(ref="kb_fee", text="入社要不要交钱", vector=[0.9, 0.436, 0.0]),
]


def test_most_similar_returns_best_match_above_threshold() -> None:
    assert most_similar([0.95, 0.31, 0.0], _KNOWN, 0.8).text == "入社要不要交钱"
    assert most_similar([0.0, 2.0, 0.0], _KNOWN, 0.8).ref == "kb_interview"  # 向量不必归一化


def test_most_similar_none_below_threshold_or_without_candidates() -> None:
    assert most_similar([0.0, 0.0, 1.0], _KNOWN, 0.8) is None
    assert most_similar([1.0, 0.0, 0.0], [], 0.8) is None


def test_most_similar_threshold_is_inclusive_and_tie_keeps_first() -> None:
    assert most_similar([0.8, 0.0, 0.6], _KNOWN, 0.8).ref == "kb_fee"
    twins = [KnownQuestion("a", "", [1.0, 0.0]), KnownQuestion("b", "", [1.0, 0.0])]
    assert most_similar([1.0, 0.0], twins, 0.8).ref == "a"


def test_similar_refs_lists_every_match_once_in_order() -> None:
    # kb_fee 的两种问法都达到阈值,只列一次
    assert similar_refs([0.97, 0.24, 0.0], _KNOWN, 0.8) == ["kb_fee"]
    assert similar_refs([0.7, 0.714, 0.0], _KNOWN, 0.7) == ["kb_fee", "kb_interview"]
    assert similar_refs([0.0, 0.0, 1.0], _KNOWN, 0.8) == []
