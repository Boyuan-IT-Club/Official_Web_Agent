"""题型表与周期选题记录的真库集成测试:草稿替换、状态只能往前走、范例取用的次序。

需要真 PostgreSQL(POSTGRES_URL);无 PG 时整档 skip。CI 由 ci.yml 的 postgres service 提供。
"""

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.state import qbank as qbank_store  # noqa: E402
from official_agent.state import qbank_exemplar  # noqa: E402
from official_agent.state.qbank_exemplar import ProposedExemplar  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_tables():
    previous = point_settings_at_pg()
    try:
        with psycopg.connect(PG_URL) as conn:
            qbank_exemplar.ensure_qbank_exemplar_table(conn)
        qbank_store.ensure_qbank_ready()
        yield
    finally:
        point_settings_at_pg(previous)


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL)
    monkeypatch.setattr(qbank_exemplar, "_bootstrapped", True)


@pytest.fixture
def cycle_id():
    cycle = uuid.uuid4().int % 2_000_000_000 + 1
    yield cycle
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM qbank_exemplar WHERE cycle_id = %s", (cycle,))
        conn.execute("DELETE FROM qbank_pick_log WHERE cycle_id = %s", (cycle,))


def _draft(pattern: str, *, interviewers: int = 2, picks: int = 2) -> ProposedExemplar:
    return ProposedExemplar(
        category="C4_实现细节拷打",
        pattern=pattern,
        pick_count=picks,
        resume_count=2,
        interviewer_count=interviewers,
        source_refs=("ref-a", "ref-b"),
    )


def _propose(cycle_id: int, *drafts: ProposedExemplar) -> list[int]:
    return qbank_exemplar.replace_proposed(
        cycle_id=cycle_id, prompt_version="evaluation_exemplar_induce/v1", exemplars=list(drafts)
    )


def test_new_drafts_are_proposed_and_not_usable(cycle_id: int) -> None:
    """归纳出来只是草稿:不经人审,出题段取不到。"""
    (exemplar_id,) = _propose(cycle_id, _draft("让候选人说明某次方案更换的触发条件"))

    (row,) = qbank_exemplar.list_exemplars(cycle_id=cycle_id)
    usable = {r["exemplar_id"] for r in qbank_exemplar.approved_patterns(limit=1000)}

    assert (row["exemplar_id"], row["status"]) == (exemplar_id, "proposed")
    assert row["source_refs"] == ["ref-a", "ref-b"] and row["reviewed_by"] is None
    assert exemplar_id not in usable


def test_status_only_moves_forward(cycle_id: int) -> None:
    """草稿→通过→停用;停用之后不能再通过,也退不回草稿。"""
    (exemplar_id,) = _propose(cycle_id, _draft("追问某个指标是怎么测出来的"))

    assert qbank_exemplar.set_status(exemplar_id, "approved", reviewed_by=5) is True
    assert qbank_exemplar.set_status(exemplar_id, "approved", reviewed_by=6) is False  # 已经是了
    assert qbank_exemplar.set_status(exemplar_id, "retired", reviewed_by=6) is True
    assert qbank_exemplar.set_status(exemplar_id, "approved", reviewed_by=7) is False

    (row,) = qbank_exemplar.list_exemplars(cycle_id=cycle_id)
    assert (row["status"], row["reviewed_by"]) == ("retired", 6)
    with pytest.raises(ValueError):
        qbank_exemplar.set_status(exemplar_id, "proposed", reviewed_by=5)


def test_reproposing_replaces_drafts_but_keeps_reviewed_ones(cycle_id: int) -> None:
    """重新归纳一遍:旧草稿整批换掉,已经有人审过的题型不动。"""
    keep_id, _stale_id = _propose(cycle_id, _draft("甲"), _draft("乙"))
    qbank_exemplar.set_status(keep_id, "approved", reviewed_by=5)

    (new_id,) = _propose(cycle_id, _draft("丙"))

    rows = {r["exemplar_id"]: r for r in qbank_exemplar.list_exemplars(cycle_id=cycle_id)}
    assert {(r["pattern"], r["status"]) for r in rows.values()} == {
        ("甲", "approved"),
        ("丙", "proposed"),
    }
    assert set(rows) == {keep_id, new_id}


def test_approved_patterns_put_the_widest_picked_first(cycle_id: int) -> None:
    narrow, wide, draft = _propose(
        cycle_id,
        _draft("窄", interviewers=2, picks=9),
        _draft("宽", interviewers=4, picks=5),
        _draft("没审", interviewers=9, picks=9),
    )
    qbank_exemplar.set_status(narrow, "approved", reviewed_by=5)
    qbank_exemplar.set_status(wide, "approved", reviewed_by=5)

    mine = [
        row["pattern"]
        for row in qbank_exemplar.approved_patterns(limit=1000)
        if row["exemplar_id"] in (narrow, wide, draft)
    ]

    assert mine == ["宽", "窄"]
    (unreviewed,) = qbank_exemplar.list_exemplars(status="proposed", cycle_id=cycle_id)
    assert unreviewed["pattern"] == "没审"


def test_cycle_picks_come_back_in_time_order_with_parsed_refs(cycle_id: int) -> None:
    for interviewer, question in ((10, "先选的题?"), (11, "后选的题?")):
        qbank_store.record_picks(
            resume_id=7,
            cycle_id=cycle_id,
            interviewer_user_id=interviewer,
            question_refs=[
                {"ref_id": f"r{interviewer}", "category": "C1_背景与动机", "question": question}
            ],
        )

    picks = qbank_store.list_cycle_picks(cycle_id)

    assert [p["interviewer_user_id"] for p in picks] == [10, 11]
    assert picks[0]["question_ref"]["question"] == "先选的题?"
    summary = qbank_store.pick_type_summary(cycle_id)
    assert summary["cycle_id"] == cycle_id
    assert (summary["total_picks"], summary["interviewers"]) == (2, 2)


def test_reads_never_create_the_table(cycle_id: int) -> None:
    """只读的调用不做 DDL:表还没建过时,列表、取用、人审、复盘汇总都当作没有题型。

    题型范例没打开时没有人写草稿,所以库里不该因为有人看了一眼列表就多出一张表。
    """

    def _relation() -> str | None:
        with psycopg.connect(PG_URL) as conn:
            return conn.execute("SELECT to_regclass('qbank_exemplar')").fetchone()[0]

    with psycopg.connect(PG_URL) as conn:
        conn.execute("DROP TABLE qbank_exemplar")
    try:
        assert qbank_exemplar.list_exemplars(cycle_id=cycle_id) == []
        assert qbank_exemplar.approved_patterns(limit=5) == []
        assert qbank_exemplar.get_exemplar(1) is None
        assert qbank_exemplar.set_status(1, "approved", reviewed_by=5) is False
        assert qbank_store.pick_type_summary(cycle_id)["exemplars"] == []
        assert _relation() is None
    finally:
        with psycopg.connect(PG_URL) as conn:
            qbank_exemplar.ensure_qbank_exemplar_table(conn)
    assert _relation() is not None


def test_first_draft_write_creates_the_table(cycle_id: int, monkeypatch) -> None:
    """建表发生在第一次写草稿的时候。"""
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DROP TABLE qbank_exemplar")
    monkeypatch.setattr(qbank_exemplar, "_bootstrapped", False)

    (exemplar_id,) = _propose(cycle_id, _draft("追问某个指标是怎么测出来的"))

    assert qbank_exemplar.get_exemplar(exemplar_id)["status"] == "proposed"
