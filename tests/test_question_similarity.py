"""题目相似度标注单测:跨线查重的规则、通用题标注、旁路失败语义(假向量与假索引)。"""

import logging

import pytest

from official_agent.config import get_settings
from official_agent.evaluation import question_similarity as qs
from official_agent.kb.embedding import EmbeddingNotConfiguredError
from official_agent.state import qbank as qbank_store

#: 两个互相垂直的方向:同向的题算近似,垂直的不算
_SAME = [1.0, 0.0]
_OTHER = [0.0, 1.0]


def _entry(group_index: int, kind: str, role: str, **indexes: int) -> dict:
    return {"group_index": group_index, "group_kind": kind, "role": role, **indexes}


# ── 跨线查重(纯函数) ────────────────────────────────────


def test_weaker_evidence_line_is_marked_as_the_duplicate() -> None:
    """两条线问同一件事:留证据强的(仓线),简历线那道记为重复。"""
    entries = [
        _entry(0, "cv_dive", "reserve", reserve_index=0),
        _entry(1, "repo", "chain", chain_index=0, layer_index=0),
    ]
    assert qs.find_cross_line_duplicates(entries, [_SAME, _SAME], min_similarity=0.9) == {0: 1}


def test_same_evidence_kind_keeps_the_earlier_line() -> None:
    entries = [
        _entry(0, "repo", "reserve", reserve_index=0),
        _entry(1, "repo", "reserve", reserve_index=0),
    ]
    assert qs.find_cross_line_duplicates(entries, [_SAME, _SAME], min_similarity=0.9) == {1: 0}


def test_questions_in_the_same_group_are_not_compared() -> None:
    """同组里的重复归审题者管,索引只看跨线。"""
    entries = [
        _entry(0, "repo", "reserve", reserve_index=0),
        _entry(0, "repo", "reserve", reserve_index=1),
    ]
    assert qs.find_cross_line_duplicates(entries, [_SAME, _SAME], min_similarity=0.9) == {}


def test_entry_questions_are_never_marked_but_can_be_the_stronger_one() -> None:
    """各线的开场题措辞相近是正常的,不记重复;别的题撞上开场题仍然记。"""
    entries = [
        _entry(0, "repo", "entry"),
        _entry(1, "repo", "entry"),
        _entry(2, "cv_dive", "reserve", reserve_index=0),
    ]
    found = qs.find_cross_line_duplicates(entries, [_SAME, _SAME, _SAME], min_similarity=0.9)
    assert found == {2: 0}


def test_duplicates_point_at_a_question_that_was_kept() -> None:
    """三条线问同一件事:后两道都指向留下的那道,不会指向另一道重复题。"""
    entries = [
        _entry(0, "repo", "reserve", reserve_index=0),
        _entry(1, "autograding", "question", question_index=0),
        _entry(2, "cv_dive", "reserve", reserve_index=0),
    ]
    found = qs.find_cross_line_duplicates(entries, [_SAME] * 3, min_similarity=0.9)
    assert found == {1: 0, 2: 0}


def test_duplicate_points_at_the_most_similar_kept_question() -> None:
    """留下的题里有两道都过了门槛:指向更像的那道,不是先遇到的那道。"""
    entries = [
        _entry(0, "repo", "reserve", reserve_index=0),
        _entry(0, "repo", "reserve", reserve_index=1),
        _entry(1, "cv_dive", "reserve", reserve_index=0),
    ]
    vectors = [[1.0, 0.3], [1.0, 0.0], [1.0, 0.0]]
    assert qs.find_cross_line_duplicates(entries, vectors, min_similarity=0.9) == {2: 1}


def test_dissimilar_questions_are_left_alone() -> None:
    entries = [
        _entry(0, "repo", "reserve", reserve_index=0),
        _entry(1, "cv_dive", "reserve", reserve_index=0),
    ]
    assert qs.find_cross_line_duplicates(entries, [_SAME, _OTHER], min_similarity=0.9) == {}


# ── annotate / persist ──────────────────────────────────


def _envelope() -> dict:
    """仓线(入口 + 备选)与简历线(入口 + 备选);两道备选问的是同一件事。"""
    return {
        "schema_name": "evaluation_qbank/v2",
        "groups": [
            {
                "group": "repo",
                "qbank_v2": {
                    "group": {
                        "entry": {"category": "C1_背景与动机", "question": "为什么做这个系统?"},
                        "chains": [],
                        "reserves": [{"category": "C9_变更条件", "question": "限流怎么做的?"}],
                    }
                },
            },
            {
                "group": "cv_dive",
                "qbank_v2": {
                    "group": {
                        "entry": {"category": "C1_背景与动机", "question": "讲讲你的经历?"},
                        "chains": [],
                        "reserves": [
                            {
                                "category": "C9_变更条件",
                                "question": "限流是怎么实现的?电话 13812345678",
                            }
                        ],
                    }
                },
            },
        ],
    }


#: 上面四道题的假向量:两道入口各指一个方向,两道备选同向
_VECTORS = [[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [1.0, 0.0, 0.0]]


@pytest.fixture
def index_on(monkeypatch: pytest.MonkeyPatch) -> dict:
    """打开题目索引,换上假的 embedding 与假索引;返回它们记下的调用。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "qbank_index_enabled", True)
    monkeypatch.setattr(settings, "embed_model", "test-embed")
    seen: dict = {"generic": [0, 0, 0, 0]}

    async def _embed(texts):
        seen["texts"] = texts
        return [list(v) for v in _VECTORS[: len(texts)]]

    def _count(**kwargs):
        seen["count_kwargs"] = kwargs
        return list(seen["generic"])

    def _replace(**kwargs):
        seen["replaced"] = kwargs

    monkeypatch.setattr(qs, "embed_texts", _embed)
    monkeypatch.setattr(qs.qbank_index, "count_similar_resumes", _count)
    monkeypatch.setattr(qs.qbank_index, "replace_resume_rows", _replace)
    return seen


async def test_annotate_is_off_by_default(monkeypatch) -> None:
    """开关默认关:不算向量、不碰信封。

    用「记下有没有被调」而不是「被调就抛」来判:annotate 是旁路,自己会吞掉异常,
    抛出来的断言到不了用例这里。
    """
    embedded: list[list[str]] = []

    async def _embed(texts):
        embedded.append(texts)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(qs, "embed_texts", _embed)
    monkeypatch.setattr(qs.qbank_index, "count_similar_resumes", lambda **k: [0] * 4)
    envelope = _envelope()

    assert get_settings().qbank_index_enabled is False
    assert await qs.annotate(envelope, resume_id=1, cycle_id=2026) is None
    assert embedded == []
    assert "similarity" not in envelope


async def test_annotate_marks_cross_line_duplicate_and_generic(index_on) -> None:
    """跨线重复记在证据弱的那道上;够多份其他简历出过的题记通用,入口题除外。"""
    index_on["generic"] = [5, 3, 0, 1]  # 入口题 5 份(不记)、仓线备选 3 份(记)
    envelope = _envelope()

    pending = await qs.annotate(envelope, resume_id=7, cycle_id=2026)

    assert envelope["similarity"] == {
        "embed_model": "test-embed",
        "items": [
            {"group_index": 0, "key": "reserve:0", "duplicate_of": None, "generic_resumes": 3},
            {
                "group_index": 1,
                "key": "reserve:0",
                "duplicate_of": {"group_index": 0, "key": "reserve:0"},
                "generic_resumes": 0,
            },
        ],
    }
    assert pending is not None and pending.embed_model == "test-embed"
    assert index_on["count_kwargs"]["resume_id"] == 7
    assert index_on["count_kwargs"]["cycle_id"] == 2026
    # 题目对象原样:标注只在信封顶层
    assert "similarity" not in envelope["groups"][1]["qbank_v2"]["group"]["reserves"][0]


async def test_question_text_is_masked_before_embedding(index_on) -> None:
    """题文先脱敏再出进程:送去算向量、落进索引的都不带手机号。"""
    pending = await qs.annotate(_envelope(), resume_id=7, cycle_id=2026)

    assert pending is not None
    assert all("13812345678" not in text for text in index_on["texts"])
    assert all("13812345678" not in text for text in pending.texts)


async def test_annotate_skips_quietly_without_embedding_config(index_on, monkeypatch) -> None:
    async def _not_configured(texts):
        raise EmbeddingNotConfiguredError("EMBED_* 未配置")

    monkeypatch.setattr(qs, "embed_texts", _not_configured)
    envelope = _envelope()

    assert await qs.annotate(envelope, resume_id=7, cycle_id=2026) is None
    assert "similarity" not in envelope


async def test_annotate_survives_embedding_failure(index_on, monkeypatch, caplog) -> None:
    async def _down(texts):
        raise RuntimeError("embedding 端点 502")

    monkeypatch.setattr(qs, "embed_texts", _down)
    envelope = _envelope()

    with caplog.at_level(logging.WARNING):
        assert await qs.annotate(envelope, resume_id=7, cycle_id=2026) is None
    assert "similarity" not in envelope
    assert "题目向量计算失败" in caplog.text


async def test_index_outage_only_costs_the_generic_flags(index_on, monkeypatch, caplog) -> None:
    """索引查不了(比如库里没有 pgvector):跨线查重照做,只是不标通用题。"""

    def _no_pgvector(**kwargs):
        raise RuntimeError('type "vector" does not exist')

    monkeypatch.setattr(qs.qbank_index, "count_similar_resumes", _no_pgvector)
    envelope = _envelope()

    with caplog.at_level(logging.WARNING):
        pending = await qs.annotate(envelope, resume_id=7, cycle_id=2026)

    assert pending is not None
    assert [item["key"] for item in envelope["similarity"]["items"]] == ["reserve:0"]
    assert envelope["similarity"]["items"][0]["duplicate_of"] == {
        "group_index": 0,
        "key": "reserve:0",
    }
    assert "题目索引查询失败" in caplog.text


async def test_persist_writes_rows_under_the_saved_version(index_on) -> None:
    """落库后才有题库版本:索引行的 ref_id 与该版本的可挑题视图一致。"""
    envelope = _envelope()
    pending = await qs.annotate(envelope, resume_id=7, cycle_id=2026)

    await qs.persist(pending, envelope, resume_id=7, cycle_id=2026, qbank_version=3)

    replaced = index_on["replaced"]
    pickable = qbank_store.flatten_v2_pickable(
        envelope, resume_id=7, cycle_id=2026, qbank_version=3
    )
    assert replaced["qbank_version"] == 3 and replaced["embed_model"] == "test-embed"
    assert [row.ref_id for row in replaced["rows"]] == [entry["ref_id"] for entry in pickable]
    assert [row.embedding for row in replaced["rows"]] == _VECTORS
    assert [(row.group_kind, row.role) for row in replaced["rows"]] == [
        ("repo", "entry"),
        ("repo", "reserve"),
        ("cv_dive", "entry"),
        ("cv_dive", "reserve"),
    ]
    assert all("13812345678" not in row.question for row in replaced["rows"])


async def test_persist_does_nothing_without_pending_and_swallows_failure(
    index_on, monkeypatch, caplog
) -> None:
    await qs.persist(None, _envelope(), resume_id=7, cycle_id=2026, qbank_version=1)
    assert "replaced" not in index_on

    def _down(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(qs.qbank_index, "replace_resume_rows", _down)
    envelope = _envelope()
    pending = await qs.annotate(envelope, resume_id=7, cycle_id=2026)

    with caplog.at_level(logging.WARNING):
        await qs.persist(pending, envelope, resume_id=7, cycle_id=2026, qbank_version=1)
    assert "题目索引落库失败" in caplog.text


async def test_persist_refuses_to_guess_when_counts_disagree(index_on, caplog) -> None:
    """题数与向量数对不上时不落库:宁可没有索引,也不把向量挂到别的题上。"""
    envelope = _envelope()
    pending = await qs.annotate(envelope, resume_id=7, cycle_id=2026)
    envelope["groups"].pop()

    with caplog.at_level(logging.WARNING):
        await qs.persist(pending, envelope, resume_id=7, cycle_id=2026, qbank_version=1)

    assert "replaced" not in index_on
    assert "题数与向量数不一致" in caplog.text
