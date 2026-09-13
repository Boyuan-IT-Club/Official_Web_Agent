"""B2 /admin/evaluation* 路由测试:权限闸 + 触发/列表契约(mock runner)。"""

import contextlib
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from official_agent.web.app import create_app


@contextlib.asynccontextmanager
async def _fake_checkpointer() -> AsyncIterator[None]:
    yield None


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from official_agent.config import get_settings

    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _identity(codes: list[str]) -> dict:
    return {
        "user_id": 1,
        "role": "admin",
        "role_names": ["管理员"],
        "permission_codes": codes,
        "source": "web",
    }


def _install_resolve(monkeypatch: pytest.MonkeyPatch, identity: dict) -> None:
    from official_agent.web import routes

    async def _resolve(*_a: object, **_k: object) -> dict:
        return identity

    monkeypatch.setattr(routes, "resolve", _resolve)


_AUTH = {"Authorization": "Bearer tok"}


def test_evaluation_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/agent/admin/evaluation/run", json={"cycle_id": 2026, "items": []})
    assert resp.status_code == 401


def test_evaluation_rejects_without_resume_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#177:发起初筛是执行权 evaluation:run;查看权 resume:audit 不放行。"""
    _install_resolve(monkeypatch, _identity(["agent:monitor", "kb:manage"]))
    resp = client.post(
        "/api/agent/admin/evaluation/run",
        headers=_AUTH,
        json={"cycle_id": 2026, "items": [{"resume_id": 1}]},
    )
    assert resp.status_code == 403
    assert "evaluation:run" in resp.json()["detail"]


def test_evaluation_run_rejects_view_only_permission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#177:只有 resume:audit(可看结果)不能发起运行——查看与执行分别授权。"""
    _install_resolve(monkeypatch, _identity(["resume:audit"]))
    resp = client.post(
        "/api/agent/admin/evaluation/run",
        headers=_AUTH,
        json={"cycle_id": 2026, "items": [{"resume_id": 1}]},
    )
    assert resp.status_code == 403
    assert "evaluation:run" in resp.json()["detail"]


def test_evaluation_run_submits_jobs(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, _identity(["evaluation:run", "resume:audit"]))
    captured: dict = {}

    class _FakeRunner:
        async def submit(self, cycle_id, items, *, trigger_user_id):
            captured.update(
                cycle_id=cycle_id,
                items=[i.resume_id for i in items],
                trigger_user_id=trigger_user_id,
            )
            return [1, 2]

    monkeypatch.setattr(ea.eval_runner, "get_runner", lambda: _FakeRunner())
    resp = client.post(
        "/api/agent/admin/evaluation/run",
        headers=_AUTH,
        json={
            "cycle_id": 2026,
            "items": [{"resume_id": 11}, {"resume_id": 12}],
        },
    )
    assert resp.status_code == 202
    assert resp.json() == {"job_ids": [1, 2], "submitted": 2}
    assert captured["cycle_id"] == 2026
    assert captured["items"] == [11, 12]
    assert captured["trigger_user_id"] == 1  # 触发人进审计


def test_evaluation_run_rejects_empty_items(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_resolve(monkeypatch, _identity(["evaluation:run"]))
    resp = client.post(
        "/api/agent/admin/evaluation/run",
        headers=_AUTH,
        json={"cycle_id": 2026, "items": []},
    )
    assert resp.status_code == 422


def test_evaluation_jobs_list_passes_filters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, _identity(["resume:audit"]))
    seen: dict = {}

    def _fake_list(cycle_id, *, status=None):
        seen.update(cycle_id=cycle_id, status=status)
        return [{"job_id": 1, "status": "succeeded"}]

    monkeypatch.setattr(ea.evaluation, "list_jobs", _fake_list)

    async def _fake_to_thread(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    resp = client.get(
        "/api/agent/admin/evaluation/jobs?cycle_id=2026&status=failed",
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert seen == {"cycle_id": 2026, "status": "failed"}
    assert resp.json()["items"][0]["job_id"] == 1
