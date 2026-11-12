"""引用核验守卫与守卫判定编码的单测:纯函数。"""

import pytest

from official_agent.security import citation_guard
from official_agent.security.citation_guard import check_citations
from official_agent.security.guard_verdict import format_verdicts, parse_verdicts

_RETRIEVED = ["技术部介绍", "招新流程(2026 版)"]


# ── 引用核验 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    [
        "据知识库《技术部介绍》,技术部负责官网开发。",
        "详见《技术部介绍》和《招新流程》。",  # 标题被截短
        "详见《 技术部介绍 》。",  # 书名号里有空白
        "详见《招新流程(2026 版)(修订)》。",  # 标题被补长
    ],
)
def test_citing_retrieved_sources_is_clean(reply: str) -> None:
    assert check_citations(reply, _RETRIEVED, searched=True) == "clean"


def test_citing_a_source_that_was_not_retrieved() -> None:
    reply = "据《技术部介绍》与《奖学金办法》,每学期评选奖学金;另见《补贴细则》。"
    # 报出第一个对不上的标题
    assert check_citations(reply, _RETRIEVED, searched=True) == "unverified:奖学金办法"


def test_searched_without_hits_makes_every_citation_unverified() -> None:
    assert check_citations("据《会费说明》,不收费。", [], searched=True) == "unverified:会费说明"


def test_citing_without_searching_this_turn_is_labelled_differently() -> None:
    """本轮没查知识库却引了条目:可能是上一轮查到的,也可能是编的——单独一类,便于分开看。"""
    verdict = check_citations("上面说的《技术部介绍》里有。", [], searched=False)
    assert verdict == "unsearched:技术部介绍"
    # 没查过就不看标题对不对得上:就算碰巧同名也算「没查」
    assert check_citations("见《技术部介绍》", _RETRIEVED, searched=False).startswith("unsearched:")


@pytest.mark.parametrize("reply", ["知识库里没有这部分。", "", "书名号没闭合《技术部介绍", "《》"])
def test_no_citation_means_the_guard_does_not_apply(reply: str) -> None:
    assert check_citations(reply, _RETRIEVED, searched=True) is None
    assert check_citations(reply, _RETRIEVED, searched=False) is None


def test_single_character_overlap_does_not_count_as_a_match() -> None:
    assert check_citations("据《部》的说明", _RETRIEVED, searched=True) == "unverified:部"
    assert check_citations("据《技术部介绍》", ["部"], searched=True) == "unverified:技术部介绍"


def test_overlong_or_multiline_brackets_are_not_treated_as_titles() -> None:
    long_text = "很" * 61
    assert check_citations(f"《{long_text}》", _RETRIEVED, searched=True) is None
    assert check_citations("《技术部\n介绍》", _RETRIEVED, searched=True) is None


def test_guard_name_is_stable() -> None:
    """守卫名会出现在落库的判定里,查询方按它筛:改名等于改数据契约。"""
    assert citation_guard.GUARD_NAME == "kb_citation"


# ── 守卫判定的编码 ───────────────────────────────────────


def test_format_and_parse_roundtrip_keeps_order() -> None:
    verdicts = {"fabrication_empty_tools": "clean", "kb_citation": "unverified:奖学金办法"}
    stored = format_verdicts(verdicts)
    assert stored == "fabrication_empty_tools=clean;kb_citation=unverified:奖学金办法"
    assert parse_verdicts(stored) == verdicts
    assert list(parse_verdicts(stored)) == list(verdicts)


def test_no_guard_ran_is_stored_as_null() -> None:
    assert format_verdicts({}) is None
    assert parse_verdicts(None) == {} and parse_verdicts("") == {}


def test_separators_and_newlines_inside_a_verdict_are_neutralized() -> None:
    stored = format_verdicts({"kb_citation": "unverified:招新;面试\n安排  表"})
    assert stored == "kb_citation=unverified:招新,面试 安排 表"
    assert parse_verdicts(stored) == {"kb_citation": "unverified:招新,面试 安排 表"}


def test_overlong_verdict_is_truncated() -> None:
    stored = format_verdicts({"kb_citation": "unverified:" + "长" * 500})
    assert len(stored) == len("kb_citation=") + 120


def test_verdict_containing_equals_sign_survives() -> None:
    stored = format_verdicts({"kb_citation": "unverified:a=b"})
    assert parse_verdicts(stored) == {"kb_citation": "unverified:a=b"}


@pytest.mark.parametrize("legacy", ["clean", "triggered:查询结果"])
def test_legacy_bare_value_is_read_as_the_fabrication_guard(legacy: str) -> None:
    """只有编造守卫一个写入方的时期,这一列存的是不带守卫名的判定。"""
    assert parse_verdicts(legacy) == {"fabrication_empty_tools": legacy}
