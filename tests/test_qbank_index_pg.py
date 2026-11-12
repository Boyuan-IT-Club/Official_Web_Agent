"""题目索引真库集成测试:整批替换、按简历去重计数、周期隔离、不跨模型比较。

需要带 pgvector 的真 PostgreSQL(POSTGRES_URL);连不上或装不了扩展时整档 skip。
CI 由 ci.yml 的 pgvector service 提供。
"""

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.state import qbank_index  # noqa: E402
from official_agent.state.qbank_index import IndexRow  # noqa: E402


def _pgvector_available() -> bool:
    if not pg_available():
        return False
    try:
        with psycopg.connect(PG_URL, connect_timeout=3) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return True
    except Exception:  # noqa: BLE001 — 装不了扩展即 skip
        return False


pytestmark = pytest.mark.skipif(
    not _pgvector_available(), reason="需要带 pgvector 的真 PostgreSQL(POSTGRES_URL)"
)

_MODEL = "test-embed-3d"
#: 三维假向量:同向的算近似,互相垂直的不算
_LIMITER = [1.0, 0.0, 0.0]
_QUEUE = [0.0, 1.0, 0.0]
_NEAR_LIMITER = [0.99, 0.1, 0.0]


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_table():
    previous = point_settings_at_pg()
    try:
        with psycopg.connect(PG_URL) as conn:
            qbank_index.ensure_qbank_index_table(conn)
        yield
    finally:
        point_settings_at_pg(previous)


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL)
    monkeypatch.setattr(qbank_index, "_bootstrapped", True)


@pytest.fixture
def cycle_id():
    cycle = uuid.uuid4().int % 2_000_000_000 + 1
    yield cycle
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM qbank_question_index WHERE cycle_id = %s", (cycle,))


def _row(embedding: list[float], *, role: str = "reserve") -> IndexRow:
    return IndexRow(
        ref_id=uuid.uuid4().hex[:16],
        group_kind="repo",
        role=role,
        category="C4_实现细节拷打",
        question="限流怎么做的?",
        embedding=embedding,
    )


def _index(resume_id: int, cycle_id: int, *embeddings: list[float], model: str = _MODEL) -> None:
    qbank_index.replace_resume_rows(
        resume_id=resume_id,
        cycle_id=cycle_id,
        qbank_version=1,
        embed_model=model,
        rows=[_row(e) for e in embeddings],
    )


def _count(resume_id: int, cycle_id: int, *embeddings: list[float]) -> list[int]:
    return qbank_index.count_similar_resumes(
        resume_id=resume_id,
        cycle_id=cycle_id,
        embed_model=_MODEL,
        embeddings=list(embeddings),
        min_similarity=0.9,
    )


def test_counts_other_resumes_once_each_and_never_itself(cycle_id: int) -> None:
    """按简历去重:一份简历里有两道近似题也只算一份;自己的行不算。"""
    _index(101, cycle_id, _LIMITER, _NEAR_LIMITER)  # 同一份简历两道近似题
    _index(102, cycle_id, _LIMITER)
    _index(103, cycle_id, _QUEUE)
    _index(200, cycle_id, _LIMITER)  # 发起查询的这份简历自己

    assert _count(200, cycle_id, _LIMITER, _QUEUE) == [2, 1]


def test_other_cycles_are_not_compared(cycle_id: int) -> None:
    other_cycle = cycle_id + 1
    _index(101, other_cycle, _LIMITER)
    try:
        assert _count(200, cycle_id, _LIMITER) == [0]
    finally:
        with psycopg.connect(PG_URL) as conn:
            conn.execute("DELETE FROM qbank_question_index WHERE cycle_id = %s", (other_cycle,))


def test_rows_from_another_embedding_model_are_ignored_not_fatal(cycle_id: int) -> None:
    """换过 embedding 模型后,旧行与新向量维度不同:不参与比较,也不能让查询报错。"""
    _index(101, cycle_id, [1.0, 0.0, 0.0, 0.0, 0.0], model="old-embed-5d")
    _index(102, cycle_id, _LIMITER)

    assert _count(200, cycle_id, _LIMITER) == [1]


def test_replace_swaps_out_the_previous_version(cycle_id: int) -> None:
    """重跑出题后旧版的行整批让位:这份简历不再出的题不再参与别人的检测。"""
    _index(101, cycle_id, _LIMITER, _QUEUE)
    assert _count(200, cycle_id, _LIMITER, _QUEUE) == [1, 1]

    qbank_index.replace_resume_rows(
        resume_id=101, cycle_id=cycle_id, qbank_version=2, embed_model=_MODEL, rows=[_row(_QUEUE)]
    )

    assert _count(200, cycle_id, _LIMITER, _QUEUE) == [0, 1]
    with psycopg.connect(PG_URL) as conn:
        versions = conn.execute(
            "SELECT DISTINCT qbank_version FROM qbank_question_index "
            "WHERE cycle_id = %s AND resume_id = 101",
            (cycle_id,),
        ).fetchall()
    assert [v[0] for v in versions] == [2]


def test_empty_query_needs_no_database() -> None:
    assert qbank_index.count_similar_resumes(
        resume_id=1, cycle_id=1, embed_model=_MODEL, embeddings=[], min_similarity=0.9
    ) == []
