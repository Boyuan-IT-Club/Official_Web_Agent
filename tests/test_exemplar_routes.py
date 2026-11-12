"""题型管理接口测试:权限、未启用时拒绝写、写操作先写审计(TestClient,假存储)。"""

import contextlib
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from official_agent.config import get_settings
from official_agent.web import evaluation_admin as ea
from official_agent.web.app import create_app

_AUTH = {"Authorization": "Bearer tok"}
_BASE = "/api/agent/admin/evaluation/exemplars"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, web_no_real_pg: None) -> TestClient:
    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _login(monkeypatch: pytest.MonkeyPatch, *codes: str) -> None:
    async def _resolve(*_a: object, **_k: object) -> dict:
        return {
            "user_id": 5,
            "role": "member",
            "role_names": ["管理员"],
            "permission_codes": list(codes),
            "source": "web",
        }

    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)


class _Backend:
    """假的题型表、归纳入口与审计:按发生的先后记下每一步。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, status: str = "proposed") -> None:
        self.steps: list[str] = []
        self.status = status
        self.audit_fails = False
        self.transition_allowed = True
        monkeypatch.setattr(ea.audit, "write_audit", self._audit)
        monkeypatch.setattr(ea.exemplar_mod, "propose_exemplars", self._propose)
        monkeypatch.setattr(ea.qbank_exemplar, "list_exemplars", self._list)
        monkeypatch.setattr(ea.qbank_exemplar, "get_exemplar", self._get)
        monkeypatch.setattr(ea.qbank_exemplar, "set_status", self._set_status)

    def _audit(self, **kwargs: object) -> None:
        if self.audit_fails:
            raise RuntimeError("audit db down")
        self.steps.append(f"audit:{kwargs['action']['op']}:{kwargs['decision']}")

    async def _propose(self, cycle_id: int) -> dict:
        self.steps.append(f"propose:{cycle_id}")
        return {"status": "proposed", "picked": 4, "proposed": 1, "rejected": []}

    def _list(self, *, status: str | None = None, cycle_id: int | None = None) -> list[dict]:
        self.steps.append(f"list:{status}:{cycle_id}")
        return [{"exemplar_id": 3, "pattern": "让候选人…", "status": "proposed"}]

    def _get(self, exemplar_id: int) -> dict | None:
        if exemplar_id != 3:
            return None
        return {"exemplar_id": 3, "cycle_id": 2026, "status": self.status}

    def _set_status(self, exemplar_id: int, status: str, *, reviewed_by: int) -> bool:
        self.steps.append(f"set_status:{exemplar_id}:{status}:by{reviewed_by}")
        return self.transition_allowed


@pytest.fixture
def exemplars_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "qbank_exemplars_enabled", True)


# ── 列表:只读,未启用也能看 ───────────────────────────────


def test_list_is_readable_and_reports_disabled_by_default(client, monkeypatch) -> None:
    _login(monkeypatch, "resume:audit")
    backend = _Backend(monkeypatch)

    resp = client.get(f"{_BASE}?status=proposed&cycle_id=2026", headers=_AUTH)

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False and resp.json()["total"] == 1
    assert backend.steps == ["list:proposed:2026"]


def test_list_needs_resume_audit(client, monkeypatch) -> None:
    _login(monkeypatch, "interview:evaluate")
    _Backend(monkeypatch)

    assert client.get(_BASE, headers=_AUTH).status_code == 403


# ── 写接口:权限、开关、审计先行 ───────────────────────────


@pytest.mark.parametrize(
    ("path", "body"),
    [("/propose", {"cycle_id": 2026}), ("/3/review", {"decision": "approve"})],
)
def test_write_endpoints_need_evaluation_run(client, monkeypatch, exemplars_on, path, body) -> None:
    """题型会进以后每位候选人的出题 prompt:只有查看权的人不能归纳、不能审。"""
    _login(monkeypatch, "resume:audit")
    backend = _Backend(monkeypatch)

    assert client.post(f"{_BASE}{path}", json=body, headers=_AUTH).status_code == 403
    assert backend.steps == []


@pytest.mark.parametrize(
    ("path", "body"),
    [("/propose", {"cycle_id": 2026}), ("/3/review", {"decision": "approve"})],
)
def test_write_endpoints_refuse_while_disabled(client, monkeypatch, path, body) -> None:
    """题型范例默认关:写接口一律 409,不写审计、不归纳、不改状态。"""
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch)

    resp = client.post(f"{_BASE}{path}", json=body, headers=_AUTH)

    assert resp.status_code == 409 and "未启用" in resp.json()["detail"]
    assert backend.steps == []


def test_propose_audits_before_inducing(client, monkeypatch, exemplars_on) -> None:
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch)

    resp = client.post(f"{_BASE}/propose", json={"cycle_id": 2026}, headers=_AUTH)

    assert resp.status_code == 201 and resp.json()["proposed"] == 1
    assert backend.steps == ["audit:exemplar_propose:u5:approve", "propose:2026"]


def test_propose_is_not_run_when_audit_is_down(client, monkeypatch, exemplars_on) -> None:
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch)
    backend.audit_fails = True

    resp = client.post(f"{_BASE}/propose", json={"cycle_id": 2026}, headers=_AUTH)

    assert resp.status_code == 503 and "未执行" in resp.json()["detail"]
    assert backend.steps == []


def test_propose_failure_is_reported_as_502(client, monkeypatch, exemplars_on) -> None:
    _login(monkeypatch, "evaluation:run")
    _Backend(monkeypatch)

    async def _down(cycle_id: int) -> dict:
        raise ValueError("题型归纳两次仍不合规")

    monkeypatch.setattr(ea.exemplar_mod, "propose_exemplars", _down)

    resp = client.post(f"{_BASE}/propose", json={"cycle_id": 2026}, headers=_AUTH)

    assert resp.status_code == 502 and "草稿未变动" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("decision", "new_status"), [("approve", "approved"), ("retire", "retired")]
)
def test_review_audits_before_changing_status(
    client, monkeypatch, exemplars_on, decision, new_status
) -> None:
    """人审是写操作:先写审计,再改状态;审计里记着从什么改成什么、谁审的。"""
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch)

    resp = client.post(f"{_BASE}/3/review", json={"decision": decision}, headers=_AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"exemplar_id": 3, "status": new_status}
    assert backend.steps == [
        f"audit:exemplar_review:u5:{decision}",
        f"set_status:3:{new_status}:by5",
    ]


def test_review_does_not_change_status_when_audit_is_down(
    client, monkeypatch, exemplars_on
) -> None:
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch)
    backend.audit_fails = True

    resp = client.post(f"{_BASE}/3/review", json={"decision": "approve"}, headers=_AUTH)

    assert resp.status_code == 503 and "未改动" in resp.json()["detail"]
    assert backend.steps == []


def test_review_unknown_exemplar_is_404_without_side_effects(
    client, monkeypatch, exemplars_on
) -> None:
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch)

    resp = client.post(f"{_BASE}/99/review", json={"decision": "approve"}, headers=_AUTH)

    assert resp.status_code == 404
    assert backend.steps == []


def test_review_rejects_a_transition_the_status_does_not_allow(
    client, monkeypatch, exemplars_on
) -> None:
    """已停用的题型不能再通过:状态只能往前走。"""
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch, status="retired")
    backend.transition_allowed = False

    resp = client.post(f"{_BASE}/3/review", json={"decision": "approve"}, headers=_AUTH)

    assert resp.status_code == 409 and "retired" in resp.json()["detail"]


def test_review_rejects_decisions_outside_the_two(client, monkeypatch, exemplars_on) -> None:
    _login(monkeypatch, "evaluation:run")
    backend = _Backend(monkeypatch)

    resp = client.post(f"{_BASE}/3/review", json={"decision": "proposed"}, headers=_AUTH)

    assert resp.status_code == 422
    assert backend.steps == []
