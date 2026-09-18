"""RAG #134 R2:/admin/kb* 管理 API 测试(test_admin_conversations.py 先例)。

TestClient + monkeypatch:resolve 装 admin(kb:manage)/无权身份;
kb_store 函数被 monkeypatch(不真连 PG;真库往返在 test_kb_integration.py)。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from official_agent.kb.store import (
    KbValidationError,
    SourceRecord,
)
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


def _kb_admin_identity() -> dict:
    return {
        "user_id": 2,
        "role": "admin",
        "role_names": ["管理员"],
        "permission_codes": ["kb:manage"],
        "source": "web",
    }


def _monitor_only_identity() -> dict:
    """有 agent:monitor 但没有 kb:manage——权限分离的负例。"""
    return {
        "user_id": 1,
        "role": "admin",
        "role_names": ["管理员"],
        "permission_codes": ["agent:monitor"],
        "source": "web",
    }


def _candidate_identity() -> dict:
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

    # kb_admin 复用 routes._authenticate → 其内部 resolve 查找在 routes 模块全局
    monkeypatch.setattr(routes, "resolve", _resolve)


_AUTH = {"Authorization": "Bearer tok"}


def _record(**over) -> SourceRecord:
    base = {
        "source_id": "kb_abc123",
        "title": "技术部介绍",
        "type": "doc",
        "kind": "normal",
        "tags": ["部门"],
        "enabled": True,
        "updated_by": "user:2",
        "content_md": "# 技术部",
        "chunk_count": 1,
    }
    base.update(over)
    return SourceRecord(**base)


# ── 鉴权 ────────────────────────────────────────────────


def test_kb_requires_auth(client: TestClient) -> None:
    assert client.get("/api/agent/admin/kb/sources").status_code == 401


def test_kb_rejects_candidate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_resolve(monkeypatch, _candidate_identity())
    resp = client.get("/api/agent/admin/kb/sources", headers=_AUTH)
    assert resp.status_code == 403
    assert "kb:manage" in resp.json()["detail"]


def test_kb_rejects_monitor_without_kb_manage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """权限分离:agent:monitor 不含 kb:manage(#121)。"""
    _install_resolve(monkeypatch, _monitor_only_identity())
    resp = client.get("/api/agent/admin/kb/sources", headers=_AUTH)
    assert resp.status_code == 403


# ── 列表/详情 ────────────────────────────────────────────


def test_kb_list_returns_items(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    monkeypatch.setattr(
        kb_admin.kb_store,
        "list_sources",
        lambda **kw: {
            "items": [_record(), _record(source_id="kb_def", title="FAQ", type="faq")],
            "total": 2,
            "page": 1,
            "size": 20,
        },
    )
    resp = client.get("/api/agent/admin/kb/sources", headers=_AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2 and len(data["items"]) == 2
    assert data["items"][0]["source_id"] == "kb_abc123"


def test_kb_list_passes_filters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    seen: dict = {}

    def _fake_list(**kw):
        seen.update(kw)
        return {"items": [], "total": 0, "page": 1, "size": 20}

    monkeypatch.setattr(kb_admin.kb_store, "list_sources", _fake_list)
    resp = client.get(
        "/api/agent/admin/kb/sources?page=2&size=5&kind=test&keyword=评测",
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert seen == {"page": 2, "size": 5, "kind": "test", "keyword": "评测"}


def test_kb_detail_and_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import asdict

    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    monkeypatch.setattr(
        kb_admin.kb_store, "get_source", lambda sid: _record() if sid == "kb_abc123" else None
    )
    ok = client.get("/api/agent/admin/kb/sources/kb_abc123", headers=_AUTH)
    assert ok.status_code == 200
    assert asdict(_record()) == ok.json()
    missing = client.get("/api/agent/admin/kb/sources/nope", headers=_AUTH)
    assert missing.status_code == 404


# ── 创建/更新(入库) ─────────────────────────────────────


def test_kb_create_ingests_and_returns_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    captured: dict = {}

    async def _fake_ingest(source, *, embedder=None, source_id=None):
        captured.update(source=source, source_id=source_id)
        return "kb_new1"

    monkeypatch.setattr(kb_admin.kb_store, "ingest_source", _fake_ingest)
    resp = client.post(
        "/api/agent/admin/kb/sources",
        headers=_AUTH,
        json={
            "title": "报名 FAQ",
            "type": "faq",
            "kind": "normal",
            "tags": ["招新"],
            "question": "怎么报名?",
            "answer": "官网填表",
        },
    )
    assert resp.status_code == 201
    assert resp.json() == {"source_id": "kb_new1"}
    assert captured["source_id"] is None  # 新建由 store 生成 id
    assert captured["source"].question == "怎么报名?"
    assert captured["source"].updated_by == "user:2"  # 操作人落 updated_by


def test_kb_create_invalid_body_422(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_resolve(monkeypatch, _kb_admin_identity())
    resp = client.post(
        "/api/agent/admin/kb/sources",
        headers=_AUTH,
        json={"title": "x", "type": "pdf"},  # 非法 type
    )
    assert resp.status_code == 422


def test_kb_create_validation_error_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())

    async def _bad_ingest(source, *, embedder=None, source_id=None):
        raise KbValidationError("FAQ 条目必须同时有 question 与 answer")

    monkeypatch.setattr(kb_admin.kb_store, "ingest_source", _bad_ingest)
    resp = client.post(
        "/api/agent/admin/kb/sources",
        headers=_AUTH,
        json={"title": "x", "type": "faq", "question": "问", "answer": ""},
    )
    assert resp.status_code == 400


def test_kb_create_embed_not_configured_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EMBED_* 未配置 → 503(检查点①:面板可建但入库被挡,错误可操作)。"""
    from official_agent.kb.embedding import EmbeddingNotConfiguredError
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())

    async def _no_embed(source, *, embedder=None, source_id=None):
        raise EmbeddingNotConfiguredError("embedding 未配置:需在 .env 设 EMBED_*")

    monkeypatch.setattr(kb_admin.kb_store, "ingest_source", _no_embed)
    resp = client.post(
        "/api/agent/admin/kb/sources",
        headers=_AUTH,
        json={"title": "x", "type": "doc", "content_md": "正文"},
    )
    assert resp.status_code == 503


def test_kb_update_ingests_with_explicit_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    captured: dict = {}

    async def _fake_ingest(source, *, embedder=None, source_id=None):
        captured["source_id"] = source_id
        return source_id or "kb_x"

    monkeypatch.setattr(
        kb_admin.kb_store, "get_source", lambda sid: _record() if sid == "kb_abc123" else None
    )
    monkeypatch.setattr(kb_admin.kb_store, "ingest_source", _fake_ingest)
    resp = client.put(
        "/api/agent/admin/kb/sources/kb_abc123",
        headers=_AUTH,
        json={"title": "新标题", "type": "doc", "content_md": "# 新"},
    )
    assert resp.status_code == 200
    assert captured["source_id"] == "kb_abc123"
    missing = client.put(
        "/api/agent/admin/kb/sources/nope",
        headers=_AUTH,
        json={"title": "x", "type": "doc", "content_md": "y"},
    )
    assert missing.status_code == 404


# ── 启停/删除/重嵌 ───────────────────────────────────────


def test_kb_enable_disable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    seen: dict = {}

    def _fake_set(source_id, enabled, *, updated_by):
        seen.update(source_id=source_id, enabled=enabled, updated_by=updated_by)
        return True

    monkeypatch.setattr(kb_admin.kb_store, "set_source_enabled", _fake_set)
    resp = client.put(
        "/api/agent/admin/kb/sources/kb_abc123/enabled",
        headers=_AUTH,
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json() == {"source_id": "kb_abc123", "enabled": False}
    assert seen["enabled"] is False and seen["updated_by"] == "user:2"

    monkeypatch.setattr(kb_admin.kb_store, "set_source_enabled", lambda *a, **k: False)
    assert (
        client.put(
            "/api/agent/admin/kb/sources/nope/enabled",
            headers=_AUTH,
            json={"enabled": True},
        ).status_code
        == 404
    )


def test_kb_delete(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    monkeypatch.setattr(kb_admin.kb_store, "delete_source", lambda sid: sid == "kb_abc123")
    ok = client.delete("/api/agent/admin/kb/sources/kb_abc123", headers=_AUTH)
    assert ok.status_code == 200 and ok.json()["deleted"] is True
    assert (
        client.delete("/api/agent/admin/kb/sources/nope", headers=_AUTH).status_code == 404
    )


def test_kb_reembed_reuses_stored_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重嵌:按已存内容重建向量,不改内容本身(换模型后逐条补齐)。"""
    from official_agent.web import kb_admin

    _install_resolve(monkeypatch, _kb_admin_identity())
    captured: dict = {}

    async def _fake_ingest(source, *, embedder=None, source_id=None):
        captured.update(source=source, source_id=source_id)
        return source_id

    monkeypatch.setattr(
        kb_admin.kb_store,
        "get_source",
        lambda sid: _record(content_md="# 技术部") if sid == "kb_abc123" else None,
    )
    monkeypatch.setattr(kb_admin.kb_store, "ingest_source", _fake_ingest)
    resp = client.post("/api/agent/admin/kb/sources/kb_abc123/reembed", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"source_id": "kb_abc123", "reembedded": True}
    assert captured["source_id"] == "kb_abc123"
    assert captured["source"].content_md == "# 技术部"
    assert captured["source"].title == "技术部介绍"

    missing = client.post("/api/agent/admin/kb/sources/nope/reembed", headers=_AUTH)
    assert missing.status_code == 404
