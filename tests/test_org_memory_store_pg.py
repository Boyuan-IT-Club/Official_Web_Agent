"""组织记忆数据面真库测试:自举、勾选去重、退役与续期、过期、可见范围、召回排序,
以及「发布报告 → 勾选 → 召回」整条链路在真库上的零泄漏。

需要真 PostgreSQL(POSTGRES_URL,带 pgvector);无 PG 时整文件 skip。

表建在一个一次性的 schema 里。search_path 里带着 public 是为了用到装在那里的
vector 类型——所以这里**所有**破坏性语句都写明 schema,不让名字解析落到 public
里真正的表上(POSTGRES_URL 可能指着开发库)。
"""

import contextlib
import secrets
from datetime import UTC, datetime

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.config import get_settings  # noqa: E402
from official_agent.memory import org_store, recall  # noqa: E402
from official_agent.state import pools  # noqa: E402
from official_agent.state.retrospective import report_store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")

SCHEMA = f"org_memory_test_{secrets.token_hex(4)}"
SCOPED_URL = (
    f"{PG_URL}{'&' if '?' in PG_URL else '?'}options=-c%20search_path%3D{SCHEMA}%2Cpublic"
    if PG_URL
    else ""
)
EMBED_MODEL = "it-org-memory-model"
CANARY = "蓝鲸清单"


def _reset_caches() -> None:
    pools.reset_pools()
    org_store.reset_table_cache()
    report_store.reset_table_cache()


@pytest.fixture(scope="module", autouse=True)
def _scratch_schema():
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")  # 应用启动时也会建,装在 public
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
    previous = point_settings_at_pg(SCOPED_URL)
    _reset_caches()
    try:
        yield
    finally:
        point_settings_at_pg(previous)
        _reset_caches()
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    """每个用例从没有这两张表开始;固定 embedding 模型名。"""
    monkeypatch.setattr(get_settings(), "embed_model", EMBED_MODEL)
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {SCHEMA}.org_memory")
        conn.execute(f"DROP TABLE IF EXISTS {SCHEMA}.cycle_retrospective")
    org_store.reset_table_cache()
    report_store.reset_table_cache()


def _execute(sql: str, params: tuple = ()) -> None:
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(sql, params)


def _exists(table: str) -> bool:
    with psycopg.connect(PG_URL) as conn:
        return conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (SCHEMA, table),
        ).fetchone() is not None


def _approve(content: str = "复核队列应在两天内清完。", **over) -> org_store.OrgMemory:
    fields = {
        "kind": "norm",
        "content": content,
        "audience": "admin",
        "source_cycle": 12,
        "source_report_version": 2,
        "source_lesson_index": 0,
        "metric_ids": ["screening.jobs"],
        "approved_by": 42,
        "valid_days": 365,
        "vector": None,
    }
    fields.update(over)
    return org_store.approve(**fields)


def _expire(memory_id: str) -> None:
    _execute(
        f"UPDATE {SCHEMA}.org_memory SET valid_until = now() - interval '1 day' "
        "WHERE memory_id = %s",
        (memory_id,),
    )


# ── 自举 ────────────────────────────────────────────────


def test_table_is_created_on_a_database_that_never_had_it() -> None:
    assert not _exists("org_memory")
    assert org_store.list_memories() == []  # 读也能自举
    assert _exists("org_memory")
    org_store.reset_table_cache()
    org_store.ensure_org_memory_table()  # 再跑一遍无害


# ── 勾选、退役、续期 ────────────────────────────────────


def test_approved_lesson_is_stored_with_its_provenance() -> None:
    memory = _approve(audience="member", valid_days=90)
    assert memory.memory_id.startswith("org_")
    assert (memory.kind, memory.audience, memory.status) == ("norm", "member", "approved")
    assert (memory.source_cycle, memory.source_report_version, memory.source_lesson_index) == (
        12,
        2,
        0,
    )
    assert memory.evidence == {"metric_ids": ["screening.jobs"]}
    assert memory.approved_by == 42 and memory.expired is False
    assert 89 <= (memory.valid_until - memory.approved_at).days <= 90
    assert org_store.get(memory.memory_id) == memory


def test_same_lesson_cannot_be_approved_twice_until_retired() -> None:
    first = _approve()
    with pytest.raises(org_store.AlreadyApprovedError):
        _approve(content="换个说法再勾一次。")
    _approve(source_lesson_index=1)  # 同一份报告的另一条不受影响

    assert org_store.retire(first.memory_id, retired_by=7) is True
    again = _approve(content="退役之后重新勾选。")
    assert again.memory_id != first.memory_id
    retired = org_store.get(first.memory_id)
    assert (retired.status, retired.retired_by) == ("retired", 7) and retired.retired_at


def test_retire_and_renew_only_act_on_live_memories() -> None:
    memory = _approve()
    assert org_store.retire(memory.memory_id, retired_by=7) is True
    assert org_store.retire(memory.memory_id, retired_by=7) is False  # 已退役
    assert org_store.renew(memory.memory_id, valid_days=30) is None
    assert org_store.retire("org_missing", retired_by=7) is False
    assert org_store.get("org_missing") is None


def test_expired_memory_is_flagged_and_can_be_renewed() -> None:
    memory = _approve()
    _expire(memory.memory_id)
    assert org_store.get(memory.memory_id).expired is True
    assert org_store.get(memory.memory_id).status == "approved"  # 过期不等于退役,行保留

    renewed = org_store.renew(memory.memory_id, valid_days=30)
    assert renewed.expired is False
    assert 29 <= (renewed.valid_until - datetime.now(UTC)).days <= 30


def test_list_newest_first_and_by_status() -> None:
    old = _approve(content="先勾的", source_lesson_index=0)
    new = _approve(content="后勾的", source_lesson_index=1)
    org_store.retire(old.memory_id, retired_by=7)
    assert [m.content for m in org_store.list_memories()] == ["后勾的", "先勾的"]
    assert [m.memory_id for m in org_store.list_memories(status="approved")] == [new.memory_id]
    assert [m.memory_id for m in org_store.list_memories(status="retired")] == [old.memory_id]


def test_lessons_from_other_cycles_are_listed_for_review() -> None:
    """下一份报告要列出往届没退役的教训(含已过期的),不含本周期自己的和已退役的。"""
    from_11 = _approve(content="周期 11 的教训", source_cycle=11)
    expired_10 = _approve(content="周期 10 的旧教训", source_cycle=10)
    _expire(expired_10.memory_id)
    retired_11 = _approve(content="已退役的", source_cycle=11, source_lesson_index=1)
    org_store.retire(retired_11.memory_id, retired_by=7)
    _approve(content="本周期自己的", source_cycle=12)

    listed = org_store.list_from_other_cycles(12)

    assert [(m.content, m.expired) for m in listed] == [
        ("周期 10 的旧教训", True),
        ("周期 11 的教训", False),
    ]
    assert from_11.memory_id in {m.memory_id for m in listed}


# ── 召回 ────────────────────────────────────────────────


def test_recall_respects_audience() -> None:
    _approve(content="只给管理员", audience="admin", source_lesson_index=0)
    _approve(content="社员也可见", audience="member", source_lesson_index=1)
    everything = org_store.recall(("admin", "member"), None, limit=5)
    assert sorted(everything) == ["只给管理员", "社员也可见"]
    assert org_store.recall(("member",), None, limit=5) == ["社员也可见"]
    assert org_store.recall((), None, limit=5) == []


def test_recall_skips_retired_and_expired() -> None:
    live = _approve(content="还在生效", source_lesson_index=0)
    retired = _approve(content="已退役", source_lesson_index=1)
    expired = _approve(content="已过期", source_lesson_index=2)
    org_store.retire(retired.memory_id, retired_by=7)
    _expire(expired.memory_id)
    assert org_store.recall(("admin", "member"), None, limit=5) == ["还在生效"]
    assert live.memory_id  # 过期的规矩比没有规矩更糟:不续期就不再召回


def test_recall_orders_by_vector_then_falls_back_to_recency() -> None:
    _approve(content="关于复核队列", vector=[1.0, 0.0], source_lesson_index=0)
    _approve(content="关于宣传物料", vector=[0.0, 1.0], source_lesson_index=1)
    assert org_store.recall(("admin",), [0.0, 1.0], limit=1) == ["关于宣传物料"]
    assert org_store.recall(("admin",), [1.0, 0.0], limit=1) == ["关于复核队列"]
    # 没有问题向量:取最近勾选的
    assert org_store.recall(("admin",), None, limit=1) == ["关于宣传物料"]


def test_recall_ignores_vectors_from_another_embedding_model(monkeypatch) -> None:
    _approve(content="先勾的,向量与问题最近", vector=[1.0, 0.0], source_lesson_index=0)
    _approve(content="后勾的,向量与问题最远", vector=[0.0, 1.0], source_lesson_index=1)
    assert org_store.recall(("admin",), [1.0, 0.0], limit=1) == ["先勾的,向量与问题最近"]

    monkeypatch.setattr(get_settings(), "embed_model", "it-another-model")
    # 换了 embedding 模型:旧向量不参与比对(不同模型的向量比不出意义),
    # 退化为按时间取——拿到的是后勾的那条,而不是旧向量里「最近」的那条
    assert org_store.recall(("admin",), [1.0, 0.0], limit=1) == ["后勾的,向量与问题最远"]


def test_memory_without_a_vector_is_still_recalled() -> None:
    _approve(content="没有向量的", vector=None)
    assert org_store.recall(("admin",), [1.0, 0.0], limit=3) == ["没有向量的"]


# ── 整条链路:发布报告 → 勾选 → 召回 ─────────────────────


@contextlib.asynccontextmanager
async def _no_checkpointer():
    yield None


def _published_report(lesson: str) -> None:
    version = report_store.start_report(
        cycle_id=12,
        metrics_schema="cycle_metrics/v1",
        snapshot_version=1,
        baseline_cycle_id=None,
        baseline_snapshot_version=None,
        created_by=42,
    )
    report_store.finish_report(
        12,
        version,
        sections={
            "lessons": [{"kind": "norm", "content": lesson, "metric_ids": ["screening.jobs"]}]
        },
        report_md="# 报告",
        unverified=[],
        prompt_versions={},
        usage={},
    )
    report_store.publish_report(12, version, published_by=42)


def test_lesson_approved_through_the_api_reaches_internal_chats_only(
    monkeypatch, web_no_real_pg
) -> None:
    from fastapi.testclient import TestClient

    from official_agent.web.app import create_app

    lesson = f"技术部初筛的复核队列内部叫「{CANARY}」,应在两天内清完。"
    _published_report(lesson)
    audits: list[dict] = []

    async def _resolve(*_a, **_k):
        return {"user_id": 42, "role": "admin", "role_names": ["管理员"],
                "permission_codes": ["evaluation:run"], "source": "web"}

    async def _embed(_content: str):
        return [1.0, 0.0]

    async def _vector(_query: str):
        return [1.0, 0.0]

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _no_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    monkeypatch.setattr(
        "official_agent.state.audit.write_audit", lambda **kwargs: audits.append(kwargs)
    )
    monkeypatch.setattr("official_agent.retrospective.lessons._embed", _embed)
    monkeypatch.setattr(recall, "_query_vector", _vector)
    auth = {"Authorization": "Bearer tok"}
    base = "/api/agent/admin/org-memory"

    with TestClient(create_app()) as client:
        approved = client.post(
            f"{base}/approve",
            json={"cycle_id": 12, "version": 1, "lesson_index": 0, "audience": "admin"},
            headers=auth,
        )
        duplicate = client.post(
            f"{base}/approve", json={"cycle_id": 12, "version": 1, "lesson_index": 0}, headers=auth
        )
        listed = client.get(base, headers=auth)

    assert approved.status_code == 201, approved.text
    memory_id = approved.json()["memory_id"]
    assert duplicate.status_code == 409
    assert [i["memory_id"] for i in listed.json()["items"]] == [memory_id]
    assert [a["action"]["op"] for a in audits] == ["approve_org_memory", "approve_org_memory"]

    def _identity(role: str) -> dict:
        return {"user_id": 8, "role": role, "role_names": [role], "permission_codes": [],
                "source": "web"}

    import asyncio

    async def _recalled(role: str) -> list[str]:
        return await recall.recall_org_for(_identity(role), "复核队列有什么规矩")

    # 开关关着:谁也召回不到
    assert asyncio.run(_recalled("admin")) == []

    monkeypatch.setattr(get_settings(), "org_memory_enabled", True)
    assert asyncio.run(_recalled("admin")) == [lesson]
    assert asyncio.run(_recalled("member")) == []  # 这条只给管理员
    assert asyncio.run(_recalled("candidate")) == []
    assert asyncio.run(_recalled("unknown")) == []

    assert org_store.retire(memory_id, retired_by=42) is True
    assert asyncio.run(_recalled("admin")) == []  # 退役后立刻不再召回
