"""B6 评审队列路由测试:队列过滤/权限矩阵/采纳投一票/驳回。"""

import contextlib
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from official_agent.web import evaluation_admin as ea
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
        "user_id": 8,
        "role": "admin",
        "role_names": ["评审"],
        "permission_codes": codes,
        "source": "web",
    }


def _install_resolve(monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> None:
    from official_agent.web import routes

    async def _resolve(*_a: object, **_k: object) -> dict:
        return _identity(codes)

    monkeypatch.setattr(routes, "resolve", _resolve)


_AUTH = {"Authorization": "Bearer reviewer-jwt"}


def test_queue_rejects_without_resume_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_resolve(monkeypatch, ["interview:evaluate"])
    resp = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026", headers=_AUTH)
    assert resp.status_code == 403


def test_queue_zero_filter_queries_hard_zero(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 分/初筛不过队列 = hard_zero 过滤;不新增 status 枚举(#128)。"""
    _install_resolve(monkeypatch, ["resume:audit"])
    seen: dict = {}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            seen.update(sql=sql, params=params)

            class _Cur:
                fetchall = lambda self: [  # noqa: E731
                    {
                        "resume_id": 9,
                        "card_version": 2,
                        "status": "draft",
                        "hard_zero": True,
                        "total": 0.0,
                        "prompt_version": "v1",
                        "created_at": None,
                    }
                ]

            return _Cur()

    monkeypatch.setattr(ea.evaluation, "_conn", lambda: _Conn())
    resp = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026&queue=zero", headers=_AUTH)
    assert resp.status_code == 200
    assert seen["params"] == (2026, 2026)  # #154:外层 cycle + 子查询 user_id 归属
    assert "hard_zero = TRUE" in seen["sql"]
    assert resp.json()["items"][0]["hard_zero"] is True
    assert resp.json()["queue"] == "zero"

    bad = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026&queue=other", headers=_AUTH)
    assert bad.status_code == 400


def test_scorecard_interviewer_readonly_allowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面试官(interview:evaluate)场景内只读维卡,不破边界(#128)。"""
    _install_resolve(monkeypatch, ["interview:evaluate"])
    monkeypatch.setattr(
        ea.evaluation,
        "latest_scorecard",
        lambda r, c: {"card_version": 1, "card": {"total": 66.0}, "status": "draft"},
    )
    resp = client.get(
        "/api/agent/admin/evaluation/scorecard?resume_id=9&cycle_id=2026", headers=_AUTH
    )
    assert resp.status_code == 200


def test_adopt_puts_reviewer_vote_and_marks_adopted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """采纳:以评审本人令牌向 Backend 投一票;成功后卡才置 adopted。"""
    _install_resolve(monkeypatch, ["resume:audit"])
    captured: dict = {}

    class _FakeClient:
        async def put_as_user(self, path, json=None, user_token=""):
            captured.update(path=path, json=json, user_token=user_token)
            return {"resumeScore": 66}

    async def _fake_gbc():
        return _FakeClient()

    monkeypatch.setattr("official_agent.tools.readonly.get_backend_client", _fake_gbc)
    monkeypatch.setattr(
        ea.evaluation,
        "list_scorecards",
        lambda r, c: [{"card_version": 3, "status": "draft"}],
    )

    def _fake_set(r, c, v, status):
        captured["status"] = (r, c, v, status)
        return True

    monkeypatch.setattr(ea.evaluation, "set_scorecard_status", _fake_set)
    with patch.object(ea.audit, "write_audit", lambda **k: captured.update(audit=True)):
        resp = client.post(
            "/api/agent/admin/evaluation/adopt",
            headers=_AUTH,
            json={"resume_id": 9, "cycle_id": 2026, "score": 66},
        )
    assert resp.status_code == 200
    assert resp.json()["audit_recorded"] is True
    assert captured["path"] == "/api/resumes/9/score"
    assert captured["json"] == {"score": 66}
    assert captured["user_token"] == "reviewer-jwt"  # 评审本人身份,非 AI 服务账号
    assert captured["status"] == (9, 2026, 3, "adopted")
    assert captured.get("audit") is True


def test_adopt_missing_card_404_before_any_side_effect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#180:无卡 → 404 且发生在投票之前,零副作用。"""
    _install_resolve(monkeypatch, ["resume:audit"])

    vote_called: list = []

    class _SpyClient:
        async def put_as_user(self, path, json=None, user_token=""):
            vote_called.append(path)
            return {}

    async def _fake_gbc():
        return _SpyClient()

    monkeypatch.setattr("official_agent.tools.readonly.get_backend_client", _fake_gbc)
    monkeypatch.setattr(ea.evaluation, "list_scorecards", lambda r, c: [])
    resp = client.post(
        "/api/agent/admin/evaluation/adopt",
        headers=_AUTH,
        json={"resume_id": 9, "cycle_id": 2026, "score": 66},
    )
    assert resp.status_code == 404
    assert vote_called == [], "无卡必须在投票前拒绝"


def test_adopt_intent_audit_failure_is_clean_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#180:意图审计失败 → 503"未执行",且投票确实没发生。"""
    _install_resolve(monkeypatch, ["resume:audit"])

    vote_called: list = []

    class _SpyClient:
        async def put_as_user(self, path, json=None, user_token=""):
            vote_called.append(path)
            return {}

    async def _fake_gbc():
        return _SpyClient()

    monkeypatch.setattr("official_agent.tools.readonly.get_backend_client", _fake_gbc)
    monkeypatch.setattr(
        ea.evaluation,
        "list_scorecards",
        lambda r, c: [{"card_version": 3, "status": "draft"}],
    )

    def _boom(**k):
        raise RuntimeError("audit down")

    with patch.object(ea.audit, "write_audit", _boom):
        resp = client.post(
            "/api/agent/admin/evaluation/adopt",
            headers=_AUTH,
            json={"resume_id": 9, "cycle_id": 2026, "score": 66},
        )
    assert resp.status_code == 503
    assert vote_called == [], "意图审计失败时不得投票"


def test_adopt_backend_failure_keeps_draft(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后端投一票失败 → 502"投票未送达",卡保持 draft 可重试。"""
    from official_agent.tools.client import BackendError

    _install_resolve(monkeypatch, ["resume:audit"])

    class _FailClient:
        async def put_as_user(self, path, json=None, user_token=""):
            raise BackendError("后端连接失败")

    async def _fake_gbc():
        return _FailClient()

    monkeypatch.setattr("official_agent.tools.readonly.get_backend_client", _fake_gbc)
    monkeypatch.setattr(
        ea.evaluation,
        "list_scorecards",
        lambda r, c: [{"card_version": 3, "status": "draft"}],
    )
    set_called: list = []
    monkeypatch.setattr(
        ea.evaluation,
        "set_scorecard_status",
        lambda *a, **k: set_called.append(a) or True,
    )
    resp = client.post(
        "/api/agent/admin/evaluation/adopt",
        headers=_AUTH,
        json={"resume_id": 9, "cycle_id": 2026, "score": 66},
    )
    assert resp.status_code == 502
    assert "投票未确认送达" in resp.json()["detail"]
    assert set_called == []  # 卡态未动


def test_adopt_card_status_failure_reports_vote_landed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#180:投票落地后卡态更新失败 → 500 且如实说"票已投",不谎报未执行。"""
    _install_resolve(monkeypatch, ["resume:audit"])

    class _OkClient:
        async def put_as_user(self, path, json=None, user_token=""):
            return {}

    async def _fake_gbc():
        return _OkClient()

    monkeypatch.setattr("official_agent.tools.readonly.get_backend_client", _fake_gbc)
    monkeypatch.setattr(
        ea.evaluation,
        "list_scorecards",
        lambda r, c: [{"card_version": 3, "status": "draft"}],
    )
    monkeypatch.setattr(ea.evaluation, "set_scorecard_status", lambda *a, **k: False)
    with patch.object(ea.audit, "write_audit", lambda **k: None):
        resp = client.post(
            "/api/agent/admin/evaluation/adopt",
            headers=_AUTH,
            json={"resume_id": 9, "cycle_id": 2026, "score": 66},
        )
    assert resp.status_code == 500
    assert "投票已送达" in resp.json()["detail"]


def test_adopt_result_audit_failure_still_adopted_but_visible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#180:结果审计失败(票已投、不可撤)→ 200 adopted + audit_recorded=false。"""
    _install_resolve(monkeypatch, ["resume:audit"])

    class _OkClient:
        async def put_as_user(self, path, json=None, user_token=""):
            return {}

    async def _fake_gbc():
        return _OkClient()

    monkeypatch.setattr("official_agent.tools.readonly.get_backend_client", _fake_gbc)
    monkeypatch.setattr(
        ea.evaluation,
        "list_scorecards",
        lambda r, c: [{"card_version": 3, "status": "draft"}],
    )
    set_calls: list = []
    monkeypatch.setattr(
        ea.evaluation,
        "set_scorecard_status",
        lambda *a, **k: set_calls.append(a) or True,
    )

    def _audit_second_fails(**k):
        if k.get("action", {}).get("op") == "adopt_scorecard":
            raise RuntimeError("audit down")

    with patch.object(ea.audit, "write_audit", _audit_second_fails):
        resp = client.post(
            "/api/agent/admin/evaluation/adopt",
            headers=_AUTH,
            json={"resume_id": 9, "cycle_id": 2026, "score": 66},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "adopted"
    assert resp.json()["audit_recorded"] is False
    assert set_calls, "卡态仍要置 adopted"


def test_reject_marks_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolve(monkeypatch, ["resume:audit"])
    monkeypatch.setattr(
        ea.evaluation,
        "list_scorecards",
        lambda r, c: [{"card_version": 2, "status": "draft"}],
    )
    monkeypatch.setattr(
        ea.evaluation,
        "set_scorecard_status",
        lambda r, c, v, status: (r, c, v, status) == (9, 2026, 2, "rejected"),
    )
    resp = client.post(
        "/api/agent/admin/evaluation/reject",
        headers=_AUTH,
        json={"resume_id": 9, "cycle_id": 2026},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
