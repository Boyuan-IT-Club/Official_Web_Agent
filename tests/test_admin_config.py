"""M6 #111 /admin/config 管理 API 测试:读回显掩码/改热生效/权限。

用 FastAPI TestClient + monkeypatch(同 test_web_routes.py 先例):
- resolve 被 monkeypatch(admin / 非 admin 身份)
- get_settings / config_store 读写被 monkeypatch(不真连库)
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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """每个测试后清 get_settings 缓存——防 setenv 的假 key 被 lru_cache
    冻结污染后续测试(get_settings.cache_clear 后 env 已还原,重读即干净)。"""
    from official_agent.config import get_settings

    yield
    get_settings.cache_clear()


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


def _install_resolve(
    monkeypatch: pytest.MonkeyPatch, identity: dict
) -> None:
    from official_agent.web import routes

    async def _resolve(*_a: object, **_k: object) -> dict:
        return identity

    monkeypatch.setattr(routes, "resolve", _resolve)


def test_admin_config_requires_auth(client: TestClient) -> None:
    """无 Authorization → 401。"""
    resp = client.get("/api/agent/admin/config")
    assert resp.status_code == 401


def test_admin_config_rejects_non_monitor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 agent:monitor 身份 → 403。"""
    _install_resolve(monkeypatch, _non_admin_identity())
    resp = client.get(
        "/api/agent/admin/config", headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 403


def test_admin_config_get_echoes_masked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET: 低敏项实值 + 高敏项掩码回显(已配置/未配置+末4位)。"""
    from official_agent.config import get_settings
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    monkeypatch.setattr(
        routes, "get_all_config", lambda: {"model_strong": "deepseek-v4-flash"}
    )
    # 高敏 env: LLM_API_KEY 假设已配置(先清 get_settings 缓存让新 env 生效)
    monkeypatch.setenv("LLM_API_KEY", "sk-test1234567890abcdef")
    get_settings.cache_clear()
    resp = client.get(
        "/api/agent/admin/config", headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 200
    data = resp.json()
    # 低敏实值
    assert data["model_strong"] == "deepseek-v4-flash"
    # 高敏掩码回显(末 4 位)
    assert data["llm_api_key"]["configured"] is True
    assert data["llm_api_key"]["masked"] == "cdef"


def test_admin_config_put_applies_hot_reload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT 改低敏项 → 落库 + 热生效(get_settings 缓存失效)。"""
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    applied: list[dict] = []

    def _fake_set(key: str, value: str) -> None:
        applied.append({"key": key, "value": value})

    def _invalidate() -> None:
        pass

    monkeypatch.setattr(routes, "set_config", _fake_set)
    monkeypatch.setattr(routes, "invalidate_settings_cache", _invalidate)
    resp = client.put(
        "/api/agent/admin/config",
        json={"model_strong": "claude-sonnet-5"},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert applied == [{"key": "model_strong", "value": "claude-sonnet-5"}]


def test_admin_config_put_rejects_secret_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT 改高敏(API key)→ 400 拒绝(永不落库)。"""
    from official_agent.web import routes

    _install_resolve(monkeypatch, _admin_identity())
    resp = client.put(
        "/api/agent/admin/config",
        json={"llm_api_key": "sk-evil"},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 400