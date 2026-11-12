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


# ── 报告接口 ────────────────────────────────────────────

RUN = "/api/agent/admin/retrospective/run"
REPORT = "/api/agent/admin/retrospective/report"
REPORTS = "/api/agent/admin/retrospective/reports"
PUBLISH = "/api/agent/admin/retrospective/publish"


def _report(version: int = 1, status: str = "draft", **over):
    from official_agent.state.retrospective.report_store import ReportRecord

    fields = {
        "cycle_id": 12,
        "version": version,
        "status": status,
        "metrics_schema": catalog.METRICS_SCHEMA,
        "snapshot_version": 1,
        "baseline_cycle_id": None,
        "baseline_snapshot_version": None,
        "sections": {"writer_status": "ok", "lessons": []},
        "report_md": "# 报告正文",
        "unverified": [],
        "prompt_versions": {"writer": "retrospective_writer/v1"},
        "usage": {"input_tokens": 400, "output_tokens": 40},
        "error": None,
        "created_by": 3,
        "created_at": datetime(2026, 11, 20, 8, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 11, 20, 8, 3, tzinfo=UTC),
        "published_by": None,
        "published_at": None,
    }
    fields.update(over)
    return ReportRecord(**fields)


class _Reports:
    """替换报告表、后台生成与审计;记录调用次序。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.records: list = []
        self.events: list[tuple] = []
        self.start_error: Exception | None = None
        self.audit_error: Exception | None = None
        self.publish_error: Exception | None = None
        target = "official_agent.web.retrospective_admin"
        monkeypatch.setattr(f"{target}.report_store.start_report", self.start)
        monkeypatch.setattr(f"{target}.report_store.get_report", self.get)
        monkeypatch.setattr(f"{target}.report_store.list_reports", self.list)
        monkeypatch.setattr(f"{target}.report_store.publish_report", self.publish)
        monkeypatch.setattr(f"{target}.reports.spawn_generation", self.spawn)
        monkeypatch.setattr(f"{target}.audit.write_audit", self.audit)

    def named(self, name: str) -> list[dict]:
        return [payload for event, payload in self.events if event == name]

    def start(self, **kwargs) -> int:
        if self.start_error:
            raise self.start_error
        self.events.append(("start", kwargs))
        return 5

    def spawn(self, **kwargs) -> None:
        self.events.append(("spawn", kwargs))

    def audit(self, **kwargs) -> None:
        if self.audit_error:
            raise self.audit_error
        self.events.append(("audit", kwargs))

    def get(self, cycle_id, *, version=None, published_only=False):
        self.events.append(("get", {"version": version, "published_only": published_only}))
        matches = [
            r
            for r in self.records
            if r.cycle_id == cycle_id
            and (version is None or r.version == version)
            and (not published_only or r.status == "published")
        ]
        return max(matches, key=lambda r: r.version) if matches else None

    def list(self, cycle_id, *, published_only=False):
        self.events.append(("list", {"published_only": published_only}))
        return [
            {"cycle_id": r.cycle_id, "version": r.version, "status": r.status}
            for r in sorted(self.records, key=lambda r: -r.version)
            if r.cycle_id == cycle_id and (not published_only or r.status == "published")
        ]

    def publish(self, cycle_id, version, *, published_by):
        if self.publish_error:
            raise self.publish_error
        self.events.append(("publish", {"version": version, "published_by": published_by}))
        return _report(
            version,
            "published",
            published_by=published_by,
            published_at=datetime(2026, 11, 21, tzinfo=UTC),
        )


@pytest.fixture
def report_api(monkeypatch: pytest.MonkeyPatch) -> _Reports:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "retrospective_report_enabled", True)
    return _Reports(monkeypatch)


def test_report_generation_is_off_by_default() -> None:
    """四份 prompt 还没在真实模型上验证过:上线不等于启用。"""
    from official_agent.config import Settings

    assert Settings(_env_file=None).retrospective_report_enabled is False


def test_run_does_nothing_while_generation_is_switched_off(
    client: TestClient, monkeypatch, report_api, store, collected
) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "retrospective_report_enabled", False)
    _login(monkeypatch, ["evaluation:run"])

    resp = client.post(RUN, json={"cycle_id": 12}, headers=_AUTH)

    assert resp.status_code == 503
    assert "RETROSPECTIVE_REPORT_ENABLED" in resp.json()["detail"]
    assert collected == [] and store.saved == [] and report_api.events == []  # 连快照都不采


def test_existing_reports_stay_readable_and_publishable_while_switched_off(
    client: TestClient, monkeypatch, report_api
) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "retrospective_report_enabled", False)
    report_api.records.append(_report(2, "draft"))
    _login(monkeypatch, ["evaluation:run"])
    assert client.get(REPORT, params={"cycle_id": 12}, headers=_AUTH).status_code == 200
    published = client.post(PUBLISH, json={"cycle_id": 12, "version": 2}, headers=_AUTH)
    assert published.status_code == 200


def test_report_endpoints_require_a_token(client: TestClient, report_api, store, collected) -> None:
    assert client.post(RUN, json={"cycle_id": 12}).status_code == 401
    assert client.post(PUBLISH, json={"cycle_id": 12, "version": 1}).status_code == 401
    assert client.get(REPORT, params={"cycle_id": 12}).status_code == 401
    assert client.get(REPORTS, params={"cycle_id": 12}).status_code == 401
    assert report_api.events == [] and collected == []


@pytest.mark.parametrize("codes", [["resume:audit"], ["agent:monitor", "kb:manage"], []])
def test_generating_and_publishing_need_the_run_permission(
    client: TestClient, monkeypatch, report_api, store, collected, codes: list[str]
) -> None:
    report_api.records.append(_report())
    _login(monkeypatch, codes)
    run = client.post(RUN, json={"cycle_id": 12}, headers=_AUTH)
    publish = client.post(PUBLISH, json={"cycle_id": 12, "version": 1}, headers=_AUTH)
    assert (run.status_code, publish.status_code) == (403, 403)
    assert report_api.events == [] and collected == []  # 被拒的请求什么都没触发


@pytest.mark.parametrize("codes", [["agent:monitor", "kb:manage"], ["candidate:read:own"], []])
def test_reading_reports_needs_a_review_permission(
    client: TestClient, monkeypatch, report_api, codes: list[str]
) -> None:
    report_api.records.append(_report(status="published"))
    _login(monkeypatch, codes)
    for url in (REPORT, REPORTS):
        resp = client.get(url, params={"cycle_id": 12}, headers=_AUTH)
        assert resp.status_code == 403, url
        assert "报告正文" not in resp.text
    assert report_api.events == []


def test_run_takes_a_snapshot_reserves_a_version_and_generates_in_background(
    client: TestClient, monkeypatch, report_api, store, collected
) -> None:
    _login(monkeypatch, ["evaluation:run"], user_id=42)

    resp = client.post(RUN, json={"cycle_id": 12}, headers=_AUTH)

    assert resp.status_code == 202
    assert resp.json() == {
        "cycle_id": 12,
        "version": 5,
        "status": "generating",
        "snapshot_version": 1,
        "baseline_cycle_id": None,
    }
    assert collected == [12] and store.saved[0]["taken_by"] == 42  # 先采一次快照
    assert report_api.named("start") == [
        {
            "cycle_id": 12,
            "metrics_schema": catalog.METRICS_SCHEMA,
            "snapshot_version": 1,
            "baseline_cycle_id": None,
            "baseline_snapshot_version": None,
            "created_by": 42,
        }
    ]
    (spawned,) = report_api.named("spawn")
    assert (spawned["cycle_id"], spawned["version"]) == (12, 5)
    assert spawned["snapshot"] == _body(12)  # 交给分析员的就是刚存下的那份快照
    assert [event for event, _ in report_api.events] == ["start", "spawn"]


def test_run_against_a_baseline_hands_the_analysts_precomputed_changes(
    client: TestClient, monkeypatch, report_api, store, collected
) -> None:
    store.records.append(_record(cycle_id=11, version=4, metrics=_body(11, resumes=200)))
    _login(monkeypatch, ["evaluation:run"])

    resp = client.post(RUN, json={"cycle_id": 12, "baseline_cycle_id": 11}, headers=_AUTH)

    assert resp.status_code == 202 and resp.json()["baseline_cycle_id"] == 11
    started = report_api.named("start")[0]
    assert (started["baseline_cycle_id"], started["baseline_snapshot_version"]) == (11, 4)
    metric = report_api.named("spawn")[0]["snapshot"]["metrics"][0]
    assert (metric["value"], metric["prev"], metric["delta"]) == (213, 200, 13)


@pytest.mark.parametrize(
    ("baseline", "status"),
    [(5, 404), (12, 400), (11, 409)],  # 没有快照 / 就是本周期 / 只有别的指标定义版本
)
def test_run_with_an_unusable_baseline_starts_nothing(
    client: TestClient, monkeypatch, report_api, store, collected, baseline: int, status: int
) -> None:
    store.records.append(_record(cycle_id=11, metrics=_body(11, schema="cycle_metrics/v0")))
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(RUN, json={"cycle_id": 12, "baseline_cycle_id": baseline}, headers=_AUTH)
    assert resp.status_code == status
    assert report_api.events == []


def test_run_while_another_report_is_generating_is_409(
    client: TestClient, monkeypatch, report_api, store, collected
) -> None:
    from official_agent.state.retrospective.report_store import ReportInProgressError

    report_api.start_error = ReportInProgressError("周期 12 已有一份报告正在生成")
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(RUN, json={"cycle_id": 12}, headers=_AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (409, "周期 12 已有一份报告正在生成")
    assert report_api.named("spawn") == []


def test_run_for_an_unknown_cycle_is_404_and_reserves_nothing(
    client: TestClient, monkeypatch, report_api, store
) -> None:
    async def _collect(cycle_id: int) -> dict:
        raise metrics.CycleNotFoundError(cycle_id)

    monkeypatch.setattr(metrics, "collect_metrics", _collect)
    _login(monkeypatch, ["evaluation:run"])
    assert client.post(RUN, json={"cycle_id": 999}, headers=_AUTH).status_code == 404
    assert report_api.events == []


def test_run_reservation_failure_is_a_stable_500(
    client: TestClient, monkeypatch, report_api, store, collected
) -> None:
    report_api.start_error = RuntimeError("deadlock detected at 10.0.0.5")
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(RUN, json={"cycle_id": 12}, headers=_AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (500, "创建报告失败,请稍后重试")
    assert report_api.named("spawn") == []


def test_reviewer_with_run_permission_reads_drafts(
    client: TestClient, monkeypatch, report_api
) -> None:
    report_api.records += [_report(1, "published"), _report(2, "draft", report_md="# 草稿")]
    _login(monkeypatch, ["evaluation:run"])

    latest = client.get(REPORT, params={"cycle_id": 12}, headers=_AUTH)
    pinned = client.get(REPORT, params={"cycle_id": 12, "version": 1}, headers=_AUTH)
    listed = client.get(REPORTS, params={"cycle_id": 12}, headers=_AUTH)

    body = latest.json()
    assert (body["version"], body["status"], body["report_md"]) == (2, "draft", "# 草稿")
    assert set(body) >= {
        "cycle_id", "version", "status", "metrics_schema", "snapshot_version",
        "baseline_cycle_id", "sections", "report_md", "unverified", "prompt_versions",
        "usage", "error", "created_by", "created_at", "finished_at", "published_by",
        "published_at",
    }
    assert pinned.json()["status"] == "published"
    assert [i["version"] for i in listed.json()["items"]] == [2, 1]
    assert all(p["published_only"] is False for _, p in report_api.events)


def test_reader_without_run_permission_sees_only_published_reports(
    client: TestClient, monkeypatch, report_api
) -> None:
    """草稿是没人审过的模型产出:只有查看权的人,连它存在与否都看不到。"""
    report_api.records += [_report(1, "published"), _report(2, "draft", report_md="# 草稿")]
    _login(monkeypatch, ["resume:audit"])

    latest = client.get(REPORT, params={"cycle_id": 12}, headers=_AUTH)
    draft = client.get(REPORT, params={"cycle_id": 12, "version": 2}, headers=_AUTH)
    listed = client.get(REPORTS, params={"cycle_id": 12}, headers=_AUTH)

    assert (latest.status_code, latest.json()["version"]) == (200, 1)
    assert (draft.status_code, draft.json()["detail"]) == (404, "该周期还没有已发布的报告")
    assert "草稿" not in draft.text
    assert [i["version"] for i in listed.json()["items"]] == [1]
    assert all(p["published_only"] is True for _, p in report_api.events)


def test_report_not_found_messages(client: TestClient, monkeypatch, report_api) -> None:
    report_api.records.append(_report(1))
    _login(monkeypatch, ["evaluation:run"])
    none_yet = client.get(REPORT, params={"cycle_id": 77}, headers=_AUTH)
    no_version = client.get(REPORT, params={"cycle_id": 12, "version": 9}, headers=_AUTH)
    assert (none_yet.status_code, none_yet.json()["detail"]) == (404, "该周期还没有报告")
    assert (no_version.status_code, no_version.json()["detail"]) == (404, "指定版本的报告不存在")


def test_publish_writes_the_audit_record_before_changing_anything(
    client: TestClient, monkeypatch, report_api
) -> None:
    report_api.records.append(_report(2, "draft"))
    _login(monkeypatch, ["evaluation:run"], user_id=42)

    resp = client.post(PUBLISH, json={"cycle_id": 12, "version": 2}, headers=_AUTH)

    assert resp.status_code == 200
    assert resp.json()["status"] == "published" and resp.json()["published_by"] == 42
    assert [event for event, _ in report_api.events] == ["get", "audit", "publish"]
    (record,) = report_api.named("audit")
    assert record["action"] == {"op": "publish_retrospective", "cycle_id": 12, "version": 2}
    assert record["acting_user_id"] == 42 and record["decision"] == "u42:publish"
    assert record["channel"] == "retrospective" and record["thread_id"].startswith("retro:12:")
    assert report_api.named("publish") == [{"version": 2, "published_by": 42}]


def test_publish_is_refused_when_the_audit_record_cannot_be_written(
    client: TestClient, monkeypatch, report_api
) -> None:
    report_api.records.append(_report(2, "draft"))
    report_api.audit_error = RuntimeError("pg down")
    _login(monkeypatch, ["evaluation:run"])

    resp = client.post(PUBLISH, json={"cycle_id": 12, "version": 2}, headers=_AUTH)

    assert resp.status_code == 503
    assert resp.json()["detail"] == "审计服务不可用,报告未发布,请稍后重试"
    assert report_api.named("publish") == []  # 没有审计就不发布


@pytest.mark.parametrize(
    ("status", "expected"),
    [("generating", 409), ("failed", 409), ("published", 409), ("superseded", 409)],
)
def test_publish_of_a_non_draft_is_409_without_side_effects(
    client: TestClient, monkeypatch, report_api, status: str, expected: int
) -> None:
    report_api.records.append(_report(2, status))
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(PUBLISH, json={"cycle_id": 12, "version": 2}, headers=_AUTH)
    assert resp.status_code == expected and status in resp.json()["detail"]
    assert report_api.named("audit") == [] and report_api.named("publish") == []


def test_publish_of_a_missing_version_is_404_without_side_effects(
    client: TestClient, monkeypatch, report_api
) -> None:
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(PUBLISH, json={"cycle_id": 12, "version": 9}, headers=_AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (404, "指定版本的报告不存在")
    assert report_api.named("audit") == [] and report_api.named("publish") == []


def test_publish_losing_a_race_is_409(client: TestClient, monkeypatch, report_api) -> None:
    """验版本时还是草稿,真去发布时已被别人发布。"""
    from official_agent.state.retrospective.report_store import ReportNotPublishableError

    report_api.records.append(_report(2, "draft"))
    report_api.publish_error = ReportNotPublishableError("published")
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(PUBLISH, json={"cycle_id": 12, "version": 2}, headers=_AUTH)
    assert resp.status_code == 409 and "published" in resp.json()["detail"]


def test_publish_store_failure_is_a_stable_500(
    client: TestClient, monkeypatch, report_api
) -> None:
    report_api.records.append(_report(2, "draft"))
    report_api.publish_error = RuntimeError("could not serialize access at 10.0.0.5")
    _login(monkeypatch, ["evaluation:run"])
    resp = client.post(PUBLISH, json={"cycle_id": 12, "version": 2}, headers=_AUTH)
    assert (resp.status_code, resp.json()["detail"]) == (500, "发布报告失败,请稍后重试")


@pytest.mark.parametrize("bad", [{}, {"cycle_id": 12}, {"cycle_id": 12, "version": 0}])
def test_publish_validates_its_body(
    client: TestClient, monkeypatch, report_api, bad: dict
) -> None:
    _login(monkeypatch, ["evaluation:run"])
    assert client.post(PUBLISH, json=bad, headers=_AUTH).status_code == 422
    assert report_api.events == []
