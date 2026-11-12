"""评分条款候选数据面的真库集成测试:建表幂等、整批替换、按周期读取。"""

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.state import evaluation as ev_store  # noqa: E402
from official_agent.state.evaluation import bootstrap, clause_store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")


@pytest.fixture(scope="module", autouse=True)
def _point_at_pg():
    previous = point_settings_at_pg()
    ev_store.reset_pool()
    try:
        yield
    finally:
        point_settings_at_pg(previous)


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL)
    ev_store.reset_pool()


@pytest.fixture
def cycle_id() -> int:
    cid = uuid.uuid4().int % 2_000_000_000 + 1
    yield cid
    with psycopg.connect(PG_URL) as conn:
        conn.execute(
            "DELETE FROM evaluation_clause_candidate WHERE cycle_id IN (%s, %s)", (cid, cid + 1)
        )


def _candidate(item: str, cards: int, *, direction: str = "lenient") -> dict:
    return {
        "checklist": "技术部",
        "item": item,
        "direction": direction,
        "clause": f"关于「{item}」的补充条款候选",
        "supporting_cards": cards,
    }


def _replace(cycle_id: int, candidates: list[dict], version: str = "clauses/v1") -> int:
    return clause_store.replace_candidates(
        cycle_id, candidates, prompt_version=version, model="test-strong", created_by=7
    )


def test_table_is_created_on_first_use_and_bootstrap_is_idempotent(cycle_id: int) -> None:
    bootstrap._done.discard("clause_candidate")

    assert clause_store.list_candidates(cycle_id) == []  # 没生成过的周期
    assert "clause_candidate" in bootstrap._done
    with psycopg.connect(PG_URL, row_factory=psycopg.rows.dict_row) as conn:
        clause_store.ensure_clause_table(conn)
        clause_store.ensure_clause_table(conn)


def test_candidates_come_back_most_supported_first_with_their_provenance(cycle_id: int) -> None:
    assert (
        _replace(
            cycle_id, [_candidate("分享意愿", 3), _candidate("技术基础", 8, direction="strict")]
        )
        == 2
    )

    listed = clause_store.list_candidates(cycle_id)

    assert [(c["item"], c["supporting_cards"], c["direction"]) for c in listed] == [
        ("技术基础", 8, "strict"),
        ("分享意愿", 3, "lenient"),
    ]
    first = listed[0]
    assert first["cycle_id"] == cycle_id and first["checklist"] == "技术部"
    assert first["clause"] == "关于「技术基础」的补充条款候选"
    assert first["prompt_version"] == "clauses/v1" and first["model"] == "test-strong"
    assert first["created_by"] == 7 and first["created_at"] is not None


def test_regenerating_replaces_the_whole_batch_for_that_cycle_only(cycle_id: int) -> None:
    _replace(cycle_id, [_candidate("技术基础", 5), _candidate("分享意愿", 4)])
    _replace(cycle_id + 1, [_candidate("探索内驱力", 6)])

    _replace(cycle_id, [_candidate("社团参与意愿", 3)], version="clauses/v2")

    assert [c["item"] for c in clause_store.list_candidates(cycle_id)] == ["社团参与意愿"]
    assert [c["item"] for c in clause_store.list_candidates(cycle_id + 1)] == ["探索内驱力"]

    assert _replace(cycle_id, []) == 0  # 这次没有归纳出任何东西:清空
    assert clause_store.list_candidates(cycle_id) == []


def test_unknown_direction_is_refused_by_the_table(cycle_id: int) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _replace(cycle_id, [_candidate("技术基础", 3, direction="unsure")])
    assert clause_store.list_candidates(cycle_id) == []  # 整批回滚
