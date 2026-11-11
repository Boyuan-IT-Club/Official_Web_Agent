"""/admin/retrospective/snapshot* 路由测试:权限闸 + 触发/读取/对比的接口契约。

采集与存储都被替换(不连 PG、不连后端);真库往返在 test_retrospective_state_pg.py。
"""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from official_agent.retrospective import catalog, metrics
from official_agent.state.retrospective import SnapshotRecord
from official_agent.web.app import create_app

SNAPSHOT = "/api/agent/admin/retrospective/snapshot"
SNAPSHOTS = "/api/agent/admin/retrospective/snapshots"
_AUTH = {"Authorization": "Bearer tok"}


@contextlib.asynccontextmanager
async def _fake_checkpointer() -> AsyncIterator[None]:
    yield None


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from official_agent.config import get_settings

    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, web_no_real_pg: None) -> TestClient:
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _login(monkeypatch: pytest.MonkeyPatch, codes: list[str], *, user_id: int = 3) -> None:
    async def _resolve(*_a: object, **_k: object) -> dict:
        return {
            "user_id": user_id,
            "role": "admin",
            "role_names": ["管理员"],
            "permission_codes": codes,
            "source": "web",
        }

    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)


def _body(cycle_id: int = 12, *, resumes: int = 213, schema: str = catalog.METRICS_SCHEMA) -> dict:
    return {
        "schema": schema,
        "cycle_id": cycle_id,
        "domains": {"funnel": {"status": "ok"}},
        "metrics": [
            {
                "id": "funnel.resumes_total",
                "metric": "funnel.resumes_total",
                "value": resumes,
                "unit": "count",
                "n": resumes,
                "suppressed": False,
                "key": None,
            }
        ],
    }


def _record(cycle_id: int = 12, version: int = 1, **over) -> SnapshotRecord:
    body = over.pop("metrics", None) or _body(cycle_id)
    fields = {
        "cycle_id": cycle_id,
        "version": version,
        "metrics_schema": body["schema"],
        "metrics": body,
        "content_hash": "h" * 64,
        "taken_by": 3,
        "taken_at": datetime(2026, 11, 20, 8, 0, tzinfo=UTC),
    }
    fields.update(over)
    return SnapshotRecord(**fields)


class _Store:
    """替换 state.retrospective 的三个函数;按 (周期, 版本) 存记录。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.records: list[SnapshotRecord] = []
        self.saved: list[dict] = []
        self.next_created = True
        target = "official_agent.web.retrospective_admin.store"
        monkeypatch.setattr(f"{target}.save_snapshot", self.save)
        monkeypatch.setattr(f"{target}.get_snapshot", self.get)
        monkeypatch.setattr(f"{target}.list_snapshots", self.list)

    def save(self, **kwargs):
        self.saved.append(kwargs)
        record = _record(kwargs["cycle_id"], metrics=kwargs["metrics"], taken_by=kwargs["taken_by"])
        return record, self.next_created

    def get(self, cycle_id, *, version=None, metrics_schema=None):
        matches = [
            r
            for r in self.records
            if r.cycle_id == cycle_id
            and (version is None or r.version == version)
            and (metrics_schema is None or r.metrics_schema == metrics_schema)
        ]
        return max(matches, key=lambda r: r.version) if matches else None

    def list(self, cycle_id):
        return [
            {"cycle_id": r.cycle_id, "version": r.version, "domains": r.metrics["domains"]}
            for r in sorted(self.records, key=lambda r: -r.version)
            if r.cycle_id == cycle_id
        ]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    return _Store(monkeypatch)


@pytest.fixture
def collected(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """替换 collect_metrics;返回被采集过的周期 id 列表。"""
    calls: list[int] = []

    async def _collect(cycle_id: int) -> dict:
        calls.append(cycle_id)
        return _body(cycle_id)

    monkeypatch.setattr(metrics, "collect_metrics", _collect)
    return calls


# ── 鉴权 ────────────────────────────────────────────────


def test_every_endpoint_requires_a_token(client: TestClient, store, collected) -> None:
    assert client.post(SNAPSHOT, json={"cycle_id": 12}).status_code == 401
    assert client.get(SNAPSHOT, params={"cycle_id": 12}).status_code == 401
    assert client.get(SNAPSHOTS, params={"cycle_id": 12}).status_code == 401
    assert collected == [] and store.saved == []


@pytest.mark.parametrize(
    "codes",
    [
        ["resume:audit"],  # 查看权不够触发
        ["agent:monitor", "kb:manage"],  # 别的管理面的权限
        ["candidate:read:own"],
        [],
    ],
)
def test_trigger_is_refused_without_evaluation_run(
    client: TestClient, monkeypatch, store, collected, codes: list[str]
) -> None:
    _login(monkeypatch, codes)
    resp = client.post(SNAPSHOT, json={"cycle_id": 12}, headers=_AUTH)
    assert resp.status_code == 403
    assert "evaluation:run" in resp.json()["detail"]
    assert collected == [] and store.saved == []  # 被拒的请求不采集、不落库


@pytest.mark.parametrize("codes", [["agent:monitor", "kb:manage"], ["candidate:read:own"], []])
def test_read_is_refused_without_review_permission(
    client: TestClient, monkeypatch, store, codes: list[str]
) -> None:
    store.records.append(_record())
    _login(monkeypatch, codes)
    for url in (SNAPSHOT, SNAPSHOTS):
        resp = client.get(url, params={"cycle_id": 12}, headers=_AUTH)
        assert resp.status_code == 403, url
        assert "funnel.resumes_total" not in resp.text


@pytest.mark.parametrize("codes", [["resume:audit"], ["evaluation:run"]])
def test_read_is_allowed_with_either_review_permission(
    client: TestClient, monkeypatch, store, codes: list[str]
) -> None:
    store.records.append(_record())
    _login(monkeypatch, codes)
    assert client.get(SNAPSHOT, params={"cycle_id": 12}, headers=_AUTH).status_code == 200
    assert client.get(SNAPSHOTS, params={"cycle_id": 12}, headers=_AUTH).status_code == 200


# ── 触发采集 ────────────────────────────────────────────


def test_trigger_collects_saves_and_returns_the_snapshot(
    client: TestClient, monkeypatch, store, collected
) -> None:
    _login(monkeypatch, ["evaluation:run"], user_id=42)

    resp = client.post(SNAPSHOT, json={"cycle_id": 12}, headers=_AUTH)

    assert resp.status_code == 200
    assert collected == [12]
    (saved,) = store.saved
    assert saved["cycle_id"] == 12
    assert saved["metrics_schema"] == catalog.METRICS_SCHEMA
    assert saved["taken_by"] == 42  # 采集人取自登录身份,不由请求体指定
    assert saved["metrics"] == _body(12)
    body = resp.json()
    assert body["created"] is True
    assert (body["cycle_id"], body["version"], body["taken_by"]) == (12, 1, 42)
    assert body["metrics_schema"] == catalog.METRICS_SCHEMA
    assert body["snapshot"]["metrics"][0]["value"] == 213


def test_trigger_reports_when_nothing_changed(
    client: TestClient, monkeypatch, store, collected
) -> None:
    store.next_created = False
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(SNAPSHOT, json={"cycle_id": 12}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["created"] is False


def test_trigger_unknown_cycle_is_404_and_saves_nothing(
    client: TestClient, monkeypatch, store
) -> None:
    async def _collect(cycle_id: int) -> dict:
        raise metrics.CycleNotFoundError(cycle_id)

    monkeypatch.setattr(metrics, "collect_metrics", _collect)
    _login(monkeypatch, ["evaluation:run"])

    resp = client.post(SNAPSHOT, json={"cycle_id": 999}, headers=_AUTH)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "后端查不到周期 999,未采集"
    assert store.saved == []


def test_trigger_unexpected_failure_does_not_leak_internals(
    client: TestClient, monkeypatch, store
) -> None:
    async def _collect(_cycle_id: int) -> dict:
        raise RuntimeError('relation "evaluation_job" does not exist at /srv/app/x.py')

    monkeypatch.setattr(metrics, "collect_metrics", _collect)
    _login(monkeypatch, ["evaluation:run"])

    resp = client.post(SNAPSHOT, json={"cycle_id": 12}, headers=_AUTH)

    assert resp.status_code == 500
    assert resp.json()["detail"] == "采集指标失败,请稍后重试"
    assert store.saved == []


def test_trigger_save_failure_is_reported_as_not_saved(
    client: TestClient, monkeypatch, store, collected
) -> None:
    def _fail(**_kwargs):
        raise RuntimeError("connection refused 10.0.0.5:5432")

    monkeypatch.setattr("official_agent.web.retrospective_admin.store.save_snapshot", _fail)
    _login(monkeypatch, ["evaluation:run"])

    resp = client.post(SNAPSHOT, json={"cycle_id": 12}, headers=_AUTH)

    assert resp.status_code == 500
    assert resp.json()["detail"] == "保存指标快照失败,请稍后重试"


@pytest.mark.parametrize("bad", [{}, {"cycle_id": 0}, {"cycle_id": "十二"}])
def test_trigger_validates_the_cycle_id(
    client: TestClient, monkeypatch, store, collected, bad: dict
) -> None:
    _login(monkeypatch, ["evaluation:run"])
    assert client.post(SNAPSHOT, json=bad, headers=_AUTH).status_code == 422
    assert collected == []


# ── 读取 ────────────────────────────────────────────────


def test_read_returns_latest_version_by_default(client: TestClient, monkeypatch, store) -> None:
    store.records += [
        _record(version=1, metrics=_body(resumes=200)),
        _record(version=2, metrics=_body(resumes=213)),
    ]
    _login(monkeypatch, ["resume:audit"])

    latest = client.get(SNAPSHOT, params={"cycle_id": 12}, headers=_AUTH).json()
    assert latest["version"] == 2
    assert latest["snapshot"]["metrics"][0]["value"] == 213
    assert "prev" not in latest["snapshot"]["metrics"][0]  # 没指定基线就不带差值

    pinned = client.get(SNAPSHOT, params={"cycle_id": 12, "version": 1}, headers=_AUTH).json()
    assert pinned["version"] == 1
    assert pinned["snapshot"]["metrics"][0]["value"] == 200


def test_read_missing_snapshot_is_404(client: TestClient, monkeypatch, store) -> None:
    store.records.append(_record(version=1))
    _login(monkeypatch, ["resume:audit"])
    none_yet = client.get(SNAPSHOT, params={"cycle_id": 77}, headers=_AUTH)
    assert (none_yet.status_code, none_yet.json()["detail"]) == (404, "该周期还没有指标快照")
    no_version = client.get(SNAPSHOT, params={"cycle_id": 12, "version": 9}, headers=_AUTH)
    assert (no_version.status_code, no_version.json()["detail"]) == (
        404,
        "指定版本的指标快照不存在",
    )


def test_list_returns_versions_newest_first(client: TestClient, monkeypatch, store) -> None:
    store.records += [_record(version=1), _record(version=2), _record(cycle_id=13, version=1)]
    _login(monkeypatch, ["resume:audit"])
    body = client.get(SNAPSHOTS, params={"cycle_id": 12}, headers=_AUTH).json()
    assert body["total"] == 2
    assert [item["version"] for item in body["items"]] == [2, 1]


# ── 对着基线周期读 ──────────────────────────────────────


def test_read_with_baseline_attaches_deltas(client: TestClient, monkeypatch, store) -> None:
    store.records += [
        _record(cycle_id=12, metrics=_body(12, resumes=200)),
        _record(cycle_id=13, metrics=_body(13, resumes=236)),
    ]
    _login(monkeypatch, ["resume:audit"])

    resp = client.get(SNAPSHOT, params={"cycle_id": 13, "baseline_cycle_id": 12}, headers=_AUTH)

    assert resp.status_code == 200
    snapshot = resp.json()["snapshot"]
    metric = snapshot["metrics"][0]
    assert (metric["value"], metric["prev"], metric["delta"], metric["delta_ratio"]) == (
        236,
        200,
        36,
        0.18,
    )
    assert snapshot["baseline"] == {"cycle_id": 12, "schema": catalog.METRICS_SCHEMA}


def test_read_refuses_to_compare_across_metric_definitions(
    client: TestClient, monkeypatch, store
) -> None:
    """基线周期只有旧版指标定义的快照:409,不硬比。"""
    store.records += [
        _record(cycle_id=12, metrics=_body(12, schema="cycle_metrics/v0")),
        _record(cycle_id=13, metrics=_body(13)),
    ]
    _login(monkeypatch, ["resume:audit"])

    resp = client.get(SNAPSHOT, params={"cycle_id": 13, "baseline_cycle_id": 12}, headers=_AUTH)

    assert resp.status_code == 409
    assert catalog.METRICS_SCHEMA in resp.json()["detail"]


def test_read_picks_the_baseline_version_with_the_same_definition(
    client: TestClient, monkeypatch, store
) -> None:
    """基线周期最新一版换了定义,但留有同定义的旧版:用旧版对比。"""
    store.records += [
        _record(cycle_id=12, version=1, metrics=_body(12, resumes=200)),
        _record(cycle_id=12, version=2, metrics=_body(12, resumes=1, schema="cycle_metrics/v9")),
        _record(cycle_id=13, metrics=_body(13, resumes=236)),
    ]
    _login(monkeypatch, ["resume:audit"])
    resp = client.get(SNAPSHOT, params={"cycle_id": 13, "baseline_cycle_id": 12}, headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["snapshot"]["metrics"][0]["prev"] == 200


def test_read_with_unknown_or_self_baseline(client: TestClient, monkeypatch, store) -> None:
    store.records.append(_record(cycle_id=13, metrics=_body(13)))
    _login(monkeypatch, ["resume:audit"])
    missing = client.get(SNAPSHOT, params={"cycle_id": 13, "baseline_cycle_id": 5}, headers=_AUTH)
    assert (missing.status_code, missing.json()["detail"]) == (404, "基线周期还没有指标快照")
    itself = client.get(SNAPSHOT, params={"cycle_id": 13, "baseline_cycle_id": 13}, headers=_AUTH)
    assert itself.status_code == 400


def test_read_store_failure_is_a_stable_500(client: TestClient, monkeypatch, store) -> None:
    def _fail(*_a, **_k):
        raise RuntimeError("SELECT ... FROM cycle_metrics_snapshot failed: timeout")

    monkeypatch.setattr("official_agent.web.retrospective_admin.store.get_snapshot", _fail)
    monkeypatch.setattr("official_agent.web.retrospective_admin.store.list_snapshots", _fail)
    _login(monkeypatch, ["resume:audit"])
    for url in (SNAPSHOT, SNAPSHOTS):
        resp = client.get(url, params={"cycle_id": 12}, headers=_AUTH)
        assert (resp.status_code, resp.json()["detail"]) == (500, "查询指标快照失败,请稍后重试")
