"""评审队列路由测试:队列过滤/权限矩阵/采纳投一票/驳回。"""

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


@pytest.fixture(autouse=True)
def _empty_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """分级要读全周期的卡;缺省给空池,需要分级的用例自己覆盖。"""
    monkeypatch.setattr(ea.evaluation, "list_pool_entries", lambda cycle_id: [])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, web_no_real_pg: None) -> TestClient:
    # web_no_real_pg(tests/conftest.py):lifespan 自举/启动恢复与路由侧
    # fail-closed 审计都不落真 PG——本文件断言 200/502,无 PG 时会被顶成 503。
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

    async def _resolve(*_a: object, **_k: object) -> dict:
        return _identity(codes)

    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)


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
    """重点复核队列 = hard_zero 过滤;不新增 status 枚举。"""
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

    monkeypatch.setattr(
        "official_agent.state.evaluation._connection._conn", lambda: _Conn()
    )
    resp = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026&queue=zero", headers=_AUTH)
    assert resp.status_code == 200
    # 内层 cycle + user_id 归属 + 历史决策标记,三处都按周期限定;末尾是分页
    assert seen["params"] == (2026, 2026, 2026, 200, 0)
    assert "hard_zero = TRUE" in seen["sql"]
    assert "LIMIT %s OFFSET %s" in seen["sql"]
    assert resp.json()["items"][0]["hard_zero"] is True
    assert resp.json()["queue"] == "zero"

    bad = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026&queue=other", headers=_AUTH)
    assert bad.status_code == 400


def test_scorecard_interviewer_readonly_allowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面试官(interview:evaluate)场景内只读维卡,不破边界。"""
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
    """无卡 → 404 且发生在投票之前,零副作用。"""
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
    """意图审计失败 → 503"未执行",且投票确实没发生。"""
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
    """投票落地后卡态更新失败 → 500 且如实说"票已投",不谎报未执行。"""
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
    """结果审计失败(票已投、不可撤)→ 200 adopted + audit_recorded=false。"""
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


# ── 两维等级与分数可见性(evaluation:score:view)──────────────


def _v2_row(rid: int, match: float, effort: float, dept: str = "技术部") -> dict:
    return {
        "resume_id": rid,
        "card_version": 1,
        "status": "draft",
        "hard_zero": False,
        "total": None,
        "prompt_version": "evaluation_scoring/v7",
        "created_at": None,
        "card_schema": "evaluation_scorecard/v2",
        "intended_first": dept,
        "match_score": match,
        "effort_score": effort,
        "transfer_hint": None,
    }


def _install_queue(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> None:
    monkeypatch.setattr(ea.evaluation, "list_review_queue", lambda *a, **k: rows)
    monkeypatch.setattr(
        ea.evaluation,
        "list_pool_entries",
        lambda cycle_id: [
            {
                "resume_id": r["resume_id"],
                "hard_zero": r["hard_zero"],
                "first_dept": r.get("intended_first"),
                "match_score": r.get("match_score"),
                "effort_score": r.get("effort_score"),
            }
            for r in rows
            if r.get("card_schema") == "evaluation_scorecard/v2"
        ],
    )


def test_queue_attaches_grades_and_hides_scores_without_view_permission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 evaluation:score:view → 只给两维等级,数值分不出 API。"""
    _install_resolve(monkeypatch, ["resume:audit"])
    _install_queue(monkeypatch, [_v2_row(1, 9.0, 9.0), _v2_row(2, 7.0, 4.0), _v2_row(3, 2.0, 7.0)])
    resp = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026", headers=_AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    # 池子只有 3 人:按绝对锚点划档
    assert [i["match_level"] for i in items] == ["优秀", "良好", "一般"]
    assert [i["effort_level"] for i in items] == ["优秀", "一般", "良好"]
    assert all(i["grade_basis"] == "anchor" and i["pool"] == "技术部" for i in items)
    assert all("match_score" not in i and "effort_score" not in i for i in items)
    assert [i["total"] for i in items] == [None] * 3
    assert not any(i["needs_rerun"] for i in items)


def test_queue_shows_scores_with_view_permission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持 evaluation:score:view(仅超管)→ 两维分数照常返回,等级仍带。"""
    _install_resolve(monkeypatch, ["resume:audit", "evaluation:score:view"])
    _install_queue(monkeypatch, [_v2_row(1, 9.0, 7.0)])
    item = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026", headers=_AUTH).json()[
        "items"
    ][0]
    assert item["match_score"] == 9.0 and item["effort_score"] == 7.0
    assert item["match_level"] == "优秀" and item["effort_level"] == "良好"


def test_queue_marks_legacy_cards_for_rerun(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版卡(单一总分)没有两维等级:needs_rerun,且不参与分级。"""
    _install_resolve(monkeypatch, ["resume:audit"])
    legacy = {
        "resume_id": 5,
        "card_version": 1,
        "status": "draft",
        "hard_zero": False,
        "total": 82.0,
        "prompt_version": "evaluation_scoring/v6",
        "created_at": None,
        "card_schema": "evaluation_scorecard/v1",
    }
    _install_queue(monkeypatch, [legacy])
    item = client.get("/api/agent/admin/evaluation/queue?cycle_id=2026", headers=_AUTH).json()[
        "items"
    ][0]
    assert item["needs_rerun"] is True
    assert item["match_level"] is None and item["effort_level"] is None
    assert item["total"] is None


def _v2_card() -> dict:
    return {
        "schema": "evaluation_scorecard/v2",
        "intended": {"first": "媒体部", "second": None},
        "match": [
            {
                "dept": "媒体部",
                "score": 6.0,
                "items": [
                    {"item": "相关经历作品", "met": True, "quote": "做过海报", "reason": "r"}
                ],
            }
        ],
        "match_score": 6.0,
        "effort": {"score": 8.0, "ceiling": None, "items": []},
        "effort_score": 8.0,
        "summary": "s",
        "transfer_hint": None,
        "interview_hints": [],
        "total": None,
    }


def test_scorecard_masks_scores_but_keeps_evidence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """维卡:无 view 权限 → 各处数值分剥离;判定清单与原文依据保留(评审核对要用)。"""
    _install_resolve(monkeypatch, ["interview:evaluate"])
    card = _v2_card()
    monkeypatch.setattr(
        ea.evaluation,
        "latest_scorecard",
        lambda r, c: {"card_version": 1, "card": card, "total": None, "status": "draft"},
    )
    monkeypatch.setattr(
        ea.evaluation,
        "list_pool_entries",
        lambda cycle_id: [
            {"resume_id": 9, "hard_zero": False, "first_dept": "媒体部",
             "match_score": 6.0, "effort_score": 8.0}
        ],
    )
    body = client.get(
        "/api/agent/admin/evaluation/scorecard?resume_id=9&cycle_id=2026", headers=_AUTH
    ).json()
    assert body["match_level"] == "一般" and body["effort_level"] == "良好"
    assert body["pool"] == "媒体部"
    assert "match_score" not in body["card"] and "effort_score" not in body["card"]
    assert "score" not in body["card"]["match"][0]
    assert "score" not in body["card"]["effort"]
    assert body["card"]["match"][0]["items"][0]["quote"] == "做过海报"


def test_scorecard_shows_scores_with_view_permission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_resolve(monkeypatch, ["interview:evaluate", "evaluation:score:view"])
    monkeypatch.setattr(
        ea.evaluation,
        "latest_scorecard",
        lambda r, c: {"card_version": 1, "card": _v2_card(), "total": None, "status": "draft"},
    )
    body = client.get(
        "/api/agent/admin/evaluation/scorecard?resume_id=9&cycle_id=2026", headers=_AUTH
    ).json()
    assert body["card"]["match_score"] == 6.0
    assert body["card"]["match"][0]["score"] == 6.0


def test_legacy_scorecard_strips_traits_without_view_permission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧版卡沿用旧剥离口径:特质判定可精确反推旧总分,无 view 权限不外露。"""
    _install_resolve(monkeypatch, ["interview:evaluate"])
    card = {"schema": "evaluation_scorecard/v1", "total": 66.0,
            "traits": [{"trait": "责任心", "met": True}], "met_count": 1}
    monkeypatch.setattr(
        ea.evaluation,
        "latest_scorecard",
        lambda r, c: {"card_version": 1, "card": card, "total": 66.0, "status": "draft"},
    )
    body = client.get(
        "/api/agent/admin/evaluation/scorecard?resume_id=9&cycle_id=2026", headers=_AUTH
    ).json()
    assert body["needs_rerun"] is True
    assert body["total"] is None
    assert "traits" not in body["card"] and "met_count" not in body["card"]
