"""M6 #112 /admin/conversations 管理 API 测试:列表/详情/权限/PII。

用 FastAPI TestClient + monkeypatch(同 test_admin_config.py 先例):
- resolve 被 monkeypatch(admin / 非 admin)
- list/get 查询被 monkeypatch(不真连库)
"""

from __future__ import annotations

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


def _admin_identity() -> dict:
    return {
        "user_id": 1,
        "role": "admin",
        "role_names": ["管理员"],
        "permission_codes": ["agent:monitor", "admin:manage"],
        "source": "web",
    }


def _non_admin_identity() -> dict:
    return {
        "user_id": 7,
        "role": "candidate",
        "role_names": ["申请人"],
        "permission_codes": ["candidate:read:own"],
        "source": "web",
    }


def _install_resolve(monkeypatch: pytest.MonkeyPatch, identity: dict) -> None:
    from official_agent.web import routes

    async def _resolve(*_a: object, **_k: object) -> dict:
        return identity

    monkeypatch.setattr(routes, "resolve", _resolve)


def _list_row(id_: int, user_id: int, head: str, error_code: str | None = None) -> dict:
    return {
        "id": id_,
        "thread_id": f"web:u{user_id}:abc{id_}",
        "user_id": user_id,
        "channel": "web",
        "user_message_head": head,
        "error_code": error_code,
        "created_at": "2026-09-04T10:00:00Z",
    }


def test_conversations_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/agent/admin/conversations")
    assert resp.status_code == 401


def test_conversations_rejects_non_monitor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_resolve(monkeypatch, _non_admin_identity())
    resp = client.get(
        "/api/agent/admin/conversations", headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 403


def test_conversations_list_returns_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    monkeypatch.setattr(
        routes,
        "list_conversations",
        lambda **kw: [_list_row(1, 7, "我的面试"), _list_row(2, 8, "", "model_error")],
    )
    resp = client.get(
        "/api/agent/admin/conversations", headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["user_message_head"] == "我的面试"
    assert data["items"][1]["error_code"] == "model_error"
    # 列表投影不含完整消息
    assert "user_message" not in data["items"][0]
    assert "reply_summary" not in data["items"][0]


def test_conversations_list_passes_filters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    seen: dict = {}

    def _fake_list(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(routes, "list_conversations", _fake_list)
    resp = client.get(
        "/api/agent/admin/conversations?user_id=7&limit=20",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert seen.get("user_id") == 7
    assert seen.get("limit") == 20


def test_conversations_detail_returns_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    monkeypatch.setattr(
        routes,
        "get_conversation",
        lambda id_: {
            "id": id_,
            "thread_id": "web:u7:abc1",
            "user_id": 7,
            "channel": "web",
            "user_message": "我的面试时间",
            "reply_summary": "周六 10:00",
            "tools": ["get_my_interview"],
            "duration_ms": 1234,
            "error_code": None,
            "created_at": "2026-09-04T10:00:00Z",
        },
    )
    resp = client.get(
        "/api/agent/admin/conversations/1", headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_message"] == "我的面试时间"
    assert data["tools"] == ["get_my_interview"]
    assert data["duration_ms"] == 1234


def test_conversations_detail_missing_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    monkeypatch.setattr(routes, "get_conversation", lambda id_: None)
    resp = client.get(
        "/api/agent/admin/conversations/999", headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 404


def test_conversations_detail_error_row_no_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """错误行详情:只返回 error_code + 元数据,不泄对话内容(#112 契约)。"""
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    monkeypatch.setattr(
        routes,
        "get_conversation",
        lambda id_: {
            "id": id_,
            "thread_id": "web:u7:abc1",
            "user_id": 7,
            "channel": "web",
            "user_message": "",  # 错误行写入时已剥离
            "reply_summary": "",
            "tools": [],
            "duration_ms": 500,
            "error_code": "model_error",
            "created_at": "2026-09-04T10:00:00Z",
        },
    )
    resp = client.get(
        "/api/agent/admin/conversations/5", headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error_code"] == "model_error"
    assert data["user_message"] == ""
    assert data["reply_summary"] == ""


def test_conversations_invalid_params_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分页参数非整数 → 400(评审 #112 MINOR)。"""
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    monkeypatch.setattr(routes, "list_conversations", lambda **kw: [])
    for qs in ("user_id=abc", "limit=abc", "offset=abc"):
        resp = client.get(
            f"/api/agent/admin/conversations?{qs}",
            headers={"Authorization": "Bearer tok"},
        )
        assert resp.status_code == 400, f"{qs} 应 400"
    # 负数 limit 被钳到 1(设计:钳制非拒绝)
    resp = client.get(
        "/api/agent/admin/conversations?limit=-5",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200


def test_conversations_limit_clamped_to_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """limit 超上限被钳到 200(评审 #112 MINOR)。"""
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    seen: dict = {}
    monkeypatch.setattr(
        routes, "list_conversations", lambda **kw: seen.update(kw) or []
    )
    resp = client.get(
        "/api/agent/admin/conversations?limit=999",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert seen.get("limit") == 200