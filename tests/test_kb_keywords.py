"""检索关键词抽取:专有名词保整、疑问词与单字不进字面匹配。"""

from official_agent.kb.keywords import query_terms


def test_glossary_terms_stay_whole() -> None:
    terms = query_terms("计拔的同学能报项目部吗")
    assert "计拔" in terms and "项目部" in terms


def test_question_words_and_single_chars_dropped() -> None:
    terms = query_terms("技术部是做什么的?")
    assert terms == ["技术部"]


def test_ascii_terms_kept_and_deduplicated() -> None:
    assert query_terms("Python 和 python 的 GIL 是什么") == ["Python", "python", "GIL"]


def test_empty_query() -> None:
    assert query_terms("") == []
    assert query_terms("?!。") == []
