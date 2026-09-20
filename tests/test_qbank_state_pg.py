"""qbank 数据面真库集成测试:pick log 的事务边界与自举纪律。

无 PG 时整档 skip;CI 由 ci.yml 的 postgres service 提供。
"""

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import (  # noqa: E402
    PG_URL,
    PG_URL_WITH_LOCK_TIMEOUT,
    pg_available,
    point_settings_at_pg,
)

from official_agent.state import qbank as qbank_store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")

_ENVELOPE = {"schema_name": "evaluation_qbank/v2", "groups": []}


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_tables():
    previous = point_settings_at_pg()
    try:
        qbank_store.ensure_qbank_ready()
        yield
    finally:
        point_settings_at_pg(previous)


@pytest.fixture
def cycle_id() -> int:
    return uuid.uuid4().int % 2_000_000_000 + 1


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL)


def _cleanup(cycle_id: int) -> None:
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM qbank_pick_log WHERE cycle_id = %s", (cycle_id,))
        conn.execute("DELETE FROM interview_qbank WHERE cycle_id = %s", (cycle_id,))


def test_record_picks_is_all_or_nothing(cycle_id: int) -> None:
    """整批单事务:中途失败一条也不留。

    回归钉:逐条自开事务时,第 N 条炸掉会把前 N-1 条留在库里,接口却返
    500;面试官一重试,qbank_pick_log(无唯一约束)里就多出重复勾选。
    """
    _cleanup(cycle_id)
    refs = [
        {"ref_id": "a", "question": "架构?"},
        {"ref_id": "b", "question": "并发?"},
        {"ref_id": "c", "question": object()},  # 不可 JSON 序列化 → 第 3 条炸
    ]
    try:
        with pytest.raises(TypeError):
            qbank_store.record_picks(
                resume_id=9201,
                cycle_id=cycle_id,
                interviewer_user_id=71,
                question_refs=refs,
            )
        assert qbank_store.list_picks(9201, cycle_id) == [], "失败批次不得留下半截记录"
    finally:
        _cleanup(cycle_id)


def test_record_picks_writes_whole_batch(cycle_id: int) -> None:
    """正常批次全量落库,返回的 id 与入参同序同数。"""
    _cleanup(cycle_id)
    try:
        ids = qbank_store.record_picks(
            resume_id=9202,
            cycle_id=cycle_id,
            interviewer_user_id=72,
            question_refs=[{"ref_id": "a"}, {"ref_id": "b"}],
            schedule_id=33,
        )
        assert len(ids) == 2 and all(i > 0 for i in ids)
        picks = qbank_store.list_picks(9202, cycle_id)
        assert {p["question_ref"]["ref_id"] for p in picks} == {"a", "b"}
        assert all(p["schedule_id"] == 33 for p in picks)
    finally:
        _cleanup(cycle_id)


def test_record_pick_single_still_works(cycle_id: int) -> None:
    """单条入口(record_pick)仍是现成调用点,语义不变。"""
    _cleanup(cycle_id)
    try:
        pid = qbank_store.record_pick(
            resume_id=9203,
            cycle_id=cycle_id,
            interviewer_user_id=73,
            question_ref={"ref_id": "solo"},
        )
        assert pid > 0
        assert qbank_store.list_picks(9203, cycle_id)[0]["question_ref"]["ref_id"] == "solo"
    finally:
        _cleanup(cycle_id)


def test_data_path_takes_no_ddl_lock(cycle_id: int, monkeypatch) -> None:
    """并发写锁在手时,题库读写路径照常跑完(lock_timeout 2s 兜底)。

    回归钉:ensure_qbank_tables 曾挂在每个数据函数的事务开头,
    CREATE INDEX IF NOT EXISTS 即使 no-op 也取 ShareLock,挡住所有并发写。
    """
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL_WITH_LOCK_TIMEOUT)
    holder = psycopg.connect(PG_URL)
    holder.execute("LOCK TABLE qbank_pick_log IN ROW EXCLUSIVE MODE")
    holder.execute("LOCK TABLE interview_qbank IN ROW EXCLUSIVE MODE")
    try:
        assert qbank_store.list_picks(9204, cycle_id) == []
        assert qbank_store.latest_qbank(9204, cycle_id) is None
        qbank_store.save_qbank(
            resume_id=9204,
            cycle_id=cycle_id,
            source="guided",
            envelope=_ENVELOPE,
            prompt_version="evaluation_investigate/v1",
        )
    finally:
        holder.rollback()
        holder.close()
        _cleanup(cycle_id)


def test_bootstrap_drops_legacy_prefix_index() -> None:
    """冗余的 idx_qbank_resume 被清掉;它是 UNIQUE 约束的严格前缀。"""
    with psycopg.connect(PG_URL) as conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qbank_resume "
            "ON interview_qbank (resume_id, cycle_id)"
        )
    qbank_store.ensure_qbank_ready()
    qbank_store.ensure_qbank_ready()  # 幂等
    with psycopg.connect(PG_URL) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'interview_qbank'"
            ).fetchall()
        }
    assert "idx_qbank_resume" not in names
