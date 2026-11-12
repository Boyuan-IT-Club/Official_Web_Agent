"""指标快照表(state/retrospective/snapshot_store.py)数据层单测——mock 连接。

真库往返(版本递增、幂等、存量库自举、并发撞唯一键)在
test_retrospective_state_pg.py 集成档。
"""

import inspect
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from official_agent.state.retrospective import snapshot_store


def _mock_conn() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


@pytest.fixture(autouse=True)
def _fresh_table_cache():
    """「表已建好」按进程记忆;每个用例换了 mock 连接,先清掉。"""
    snapshot_store.reset_table_cache()
    yield
    snapshot_store.reset_table_cache()


@pytest.fixture
def conn(monkeypatch) -> MagicMock:
    conn = _mock_conn()
    monkeypatch.setattr(snapshot_store, "pooled_conn", lambda: conn)
    return conn


def _sqls(conn: MagicMock) -> list[str]:
    return [c.args[0] for c in conn.execute.call_args_list]


def _row(version: int, metrics: dict, **over) -> dict:
    base = {
        "cycle_id": 12,
        "version": version,
        "metrics_schema": "cycle_metrics/v1",
        "metrics": metrics,
        "content_hash": snapshot_store.content_hash(metrics),
        "taken_by": 7,
        "taken_at": datetime(2026, 11, 20, tzinfo=UTC),
    }
    base.update(over)
    return base


# ── 建表 ────────────────────────────────────────────────


def test_ensure_creates_table_keyed_by_cycle_and_version(conn) -> None:
    snapshot_store.ensure_snapshot_table()
    table_sql = next(s for s in _sqls(conn) if "CREATE TABLE" in s)
    assert "CREATE TABLE IF NOT EXISTS cycle_metrics_snapshot" in table_sql
    assert "UNIQUE (cycle_id, version)" in table_sql
    # 不挂在会话上:会话删除与保留期清理都按 thread_id 找行,这张表没有这一列
    assert "thread_id" not in table_sql


def test_ensure_runs_ddl_once_per_database(conn, monkeypatch) -> None:
    snapshot_store.ensure_snapshot_table()
    first = conn.execute.call_count
    snapshot_store.ensure_snapshot_table()
    assert conn.execute.call_count == first  # 第二次不再发 DDL

    # 换了库(集成测试指到临时库)要重新建
    monkeypatch.setattr(
        snapshot_store, "get_settings", lambda: MagicMock(postgres_url="postgresql://other/db")
    )
    snapshot_store.ensure_snapshot_table()
    assert conn.execute.call_count == first * 2


def test_ensure_failure_is_not_remembered_as_done(conn) -> None:
    conn.execute.side_effect = RuntimeError("pg down")
    with pytest.raises(RuntimeError):
        snapshot_store.ensure_snapshot_table()
    conn.execute.side_effect = None
    snapshot_store.ensure_snapshot_table()  # 下次调用要真的再建一遍
    assert any("CREATE TABLE" in s for s in _sqls(conn))


# ── 只追加、长期保留 ────────────────────────────────────


def test_store_has_no_way_to_delete_or_rewrite_a_snapshot() -> None:
    """快照是唯一的跨周期基线:本模块不提供删除或改写,任何清理路径都碰不到它。"""
    source = inspect.getsource(snapshot_store)
    for forbidden in ("DELETE FROM", "UPDATE cycle_metrics_snapshot", "TRUNCATE", "DROP TABLE"):
        assert forbidden not in source, forbidden


def test_session_cleanup_paths_do_not_reference_the_snapshot_table() -> None:
    """会话删除与保留期清理所在的模块里不出现这张表。"""
    from official_agent.web import app, routes

    for module in (app, routes):
        assert "cycle_metrics_snapshot" not in inspect.getsource(module), module.__name__


# ── 指纹 ────────────────────────────────────────────────


def test_content_hash_ignores_key_order_but_not_values() -> None:
    a = {"schema": "v1", "metrics": [{"id": "x", "value": 5}]}
    b = {"metrics": [{"value": 5, "id": "x"}], "schema": "v1"}
    assert snapshot_store.content_hash(a) == snapshot_store.content_hash(b)
    c = {"schema": "v1", "metrics": [{"id": "x", "value": 6}]}
    assert snapshot_store.content_hash(a) != snapshot_store.content_hash(c)


# ── 保存 ────────────────────────────────────────────────


def test_save_first_snapshot_is_version_one(conn) -> None:
    metrics = {"schema": "cycle_metrics/v1", "metrics": []}
    conn.execute.return_value.fetchone.side_effect = [None, _row(1, metrics)]

    record, created = snapshot_store.save_snapshot(
        cycle_id=12, metrics_schema="cycle_metrics/v1", metrics=metrics, taken_by=7
    )

    assert (record.version, created) == (1, True)
    insert_call = next(
        c for c in conn.execute.call_args_list if "INSERT INTO cycle_metrics_snapshot" in c.args[0]
    )
    cycle_id, version, schema, _, digest, taken_by = insert_call.args[1]
    assert (cycle_id, version, schema, taken_by) == (12, 1, "cycle_metrics/v1", 7)
    assert digest == snapshot_store.content_hash(metrics)


def test_save_same_content_returns_existing_version_without_insert(conn) -> None:
    metrics = {"schema": "cycle_metrics/v1", "metrics": [{"id": "x", "value": 9}]}
    conn.execute.return_value.fetchone.return_value = _row(3, metrics)

    record, created = snapshot_store.save_snapshot(
        cycle_id=12, metrics_schema="cycle_metrics/v1", metrics=metrics, taken_by=8
    )

    assert (record.version, created) == (3, False)
    assert record.taken_by == 7  # 返回的是已有那一版,不改写采集人
    assert not any("INSERT" in s for s in _sqls(conn))


def test_save_changed_content_appends_next_version(conn) -> None:
    old = {"schema": "cycle_metrics/v1", "metrics": [{"id": "x", "value": 9}]}
    new = {"schema": "cycle_metrics/v1", "metrics": [{"id": "x", "value": 10}]}
    conn.execute.return_value.fetchone.side_effect = [_row(3, old), _row(4, new)]

    record, created = snapshot_store.save_snapshot(
        cycle_id=12, metrics_schema="cycle_metrics/v1", metrics=new, taken_by=7
    )

    assert (record.version, created) == (4, True)
    insert_call = next(c for c in conn.execute.call_args_list if "INSERT" in c.args[0])
    assert insert_call.args[1][1] == 4


def test_save_retries_once_when_a_concurrent_save_took_the_version(conn) -> None:
    """两人同时触发:后到的撞唯一键,重读后发现对方那版内容相同,直接复用。"""
    import psycopg

    metrics = {"schema": "cycle_metrics/v1", "metrics": []}
    cursor = MagicMock()
    cursor.fetchone.side_effect = [None, _row(1, metrics)]
    statements = iter(
        [cursor, psycopg.errors.UniqueViolation("duplicate key"), cursor]
    )

    def _execute(*_args, **_kwargs):
        step = next(statements)
        if isinstance(step, Exception):
            raise step
        return step

    conn.execute.side_effect = _execute
    snapshot_store._ready_urls.add(snapshot_store.get_settings().postgres_url)  # 跳过建表

    record, created = snapshot_store.save_snapshot(
        cycle_id=12, metrics_schema="cycle_metrics/v1", metrics=metrics, taken_by=7
    )
    assert (record.version, created) == (1, False)


# ── 读取 ────────────────────────────────────────────────


def test_get_latest_orders_by_version_desc(conn) -> None:
    conn.execute.return_value.fetchone.return_value = _row(2, {"schema": "cycle_metrics/v1"})
    record = snapshot_store.get_snapshot(12)
    assert record is not None and record.version == 2
    sql, params = conn.execute.call_args.args
    assert "ORDER BY version DESC LIMIT 1" in sql
    assert params == [12]


def test_get_can_pin_version_and_metric_schema(conn) -> None:
    conn.execute.return_value.fetchone.return_value = None
    assert snapshot_store.get_snapshot(12, version=5, metrics_schema="cycle_metrics/v1") is None
    sql, params = conn.execute.call_args.args
    assert "version = %s" in sql and "metrics_schema = %s" in sql
    assert params == [12, 5, "cycle_metrics/v1"]


def test_list_does_not_load_metric_bodies(conn) -> None:
    conn.execute.return_value.fetchall.return_value = [
        {"cycle_id": 12, "version": 2, "domains": {"kb": {"status": "unavailable"}}},
    ]
    items = snapshot_store.list_snapshots(12)
    assert items[0]["domains"] == {"kb": {"status": "unavailable"}}
    sql = conn.execute.call_args.args[0]
    assert "metrics->'domains'" in sql
    assert "SELECT cycle_id, version, metrics_schema, content_hash" in sql
