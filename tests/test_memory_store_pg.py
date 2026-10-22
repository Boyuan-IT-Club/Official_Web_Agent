"""长期记忆数据面真库测试:按用户隔离、增改删、召回排序、条数上限、按会话删除、过期。"""

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.memory import store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    from official_agent.config import get_settings

    previous = point_settings_at_pg()
    monkeypatch.setattr(get_settings(), "embed_model", "it-memory-model")
    store.ensure_memory_table()
    store.mark_ready()
    yield
    point_settings_at_pg(previous)


@pytest.fixture
def owner() -> int:
    return uuid.uuid4().int % 2_000_000_000 + 1


def _add(owner: int, thread: str, *items: tuple) -> int:
    return store.apply_changes(owner, source_thread=thread, add=list(items), update=[], delete=[])


def _cleanup(*owners: int) -> None:
    with psycopg.connect(PG_URL) as conn:
        for o in owners:
            conn.execute("DELETE FROM agent_memory WHERE owner_user_id = %s", (o,))
        conn.commit()


def test_add_update_delete_are_owner_scoped(owner: int) -> None:
    other = owner + 1
    try:
        _add(owner, "web:a", ("负责技术部面试", "fact", None))
        _add(other, "web:b", ("回答先给结论", "preference", None))
        (mine,) = store.list_memories(owner)
        (theirs,) = store.list_memories(other)
        # 拿别人的 id 改/删:不生效
        assert store.apply_changes(owner, source_thread="web:a", add=[],
                                   update=[(theirs.memory_id, "篡改", None)],
                                   delete=[theirs.memory_id]) == 0
        assert store.list_memories(other)[0].content == "回答先给结论"
        store.apply_changes(owner, source_thread="web:c", add=[],
                            update=[(mine.memory_id, "负责技术部和项目部的面试", None)], delete=[])
        assert store.list_memories(owner)[0].content == "负责技术部和项目部的面试"
        assert store.delete_memory(owner, mine.memory_id) is True
        assert store.list_memories(owner) == []
    finally:
        _cleanup(owner, other)


def test_recall_orders_by_vector_and_falls_back_to_recency(owner: int) -> None:
    try:
        _add(
            owner,
            "web:a",
            ("偏好简短回答", "preference", [1.0, 0.0]),
            ("负责综合部活动", "fact", [0.0, 1.0]),
        )
        assert store.recall(owner, [0.0, 1.0], limit=1) == ["负责综合部活动"]
        assert store.recall(owner, [1.0, 0.0], limit=1) == ["偏好简短回答"]
        assert len(store.recall(owner, None, limit=5)) == 2  # 无问题向量:按时间
    finally:
        _cleanup(owner)


def test_recall_ignores_vectors_from_other_models(owner: int, monkeypatch) -> None:
    from official_agent.config import get_settings

    try:
        _add(owner, "", ("旧模型的记忆", "fact", [1.0, 0.0]))
        monkeypatch.setattr(get_settings(), "embed_model", "it-other-model")
        # 换了 embedding 模型:旧向量不参与比对,退化为按时间仍能取到
        assert store.recall(owner, [1.0, 0.0], limit=3) == ["旧模型的记忆"]
    finally:
        _cleanup(owner)


def test_per_user_cap_evicts_oldest(owner: int, monkeypatch) -> None:
    monkeypatch.setattr(store, "MAX_PER_USER", 3)
    try:
        for i in range(5):
            _add(owner, "", (f"事实{i}", "fact", None))
        contents = [m.content for m in store.list_memories(owner)]
        assert contents == ["事实4", "事实3", "事实2"]
    finally:
        _cleanup(owner)


def test_delete_by_thread_and_purge_expired(owner: int) -> None:
    try:
        _add(owner, "web:gone", ("来自被删会话", "fact", None))
        _add(owner, "web:keep", ("保留", "fact", None))
        assert store.delete_by_thread(owner, "web:gone") == 1
        assert [m.content for m in store.list_memories(owner)] == ["保留"]
        with psycopg.connect(PG_URL) as conn:
            conn.execute(
                "UPDATE agent_memory SET updated_at = now() - interval '400 days' "
                "WHERE owner_user_id = %s",
                (owner,),
            )
            conn.commit()
        assert store.recall(owner, None) == []  # 过期不召回
        assert store.purge_expired() >= 1
        assert store.list_memories(owner) == []
    finally:
        _cleanup(owner)
