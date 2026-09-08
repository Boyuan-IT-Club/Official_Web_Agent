"""RAG #134 R1:KB 真库集成档(pgvector 容器;向量用确定性注入,不依赖真实端点)。

运行门槛:环境变量 KB_TEST_DATABASE_URL 指向带 pgvector 的库,缺省整档跳过——
单测档(test_kb_store.py)不依赖真库,CI 保持纯 mock。
本地:docker compose -f deploy/docker-compose.local.yml up -d agent-pg 后
  KB_TEST_DATABASE_URL=postgresql://postgres:agent_dev@localhost:5433/official_agent
"""

import os
import secrets

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("KB_TEST_DATABASE_URL"),
    reason="需要 KB_TEST_DATABASE_URL(本地 pgvector 容器)",
)

from official_agent.config import Settings  # noqa: E402
from official_agent.kb import store  # noqa: E402
from official_agent.kb.store import SourceInput  # noqa: E402

DIM = 2


def _patch_pg(monkeypatch) -> None:
    url = os.environ["KB_TEST_DATABASE_URL"]
    s = Settings(
        _env_file=None,
        postgres_url=url,
        embed_model="it-fake-model",
        embed_dim=DIM,
    )
    monkeypatch.setattr("official_agent.kb.store.get_settings", lambda: s)
    monkeypatch.setattr("official_agent.kb.schema.get_settings", lambda: s)


def _axis_embedder():
    """确定性向量:文本含 'ax0' → [1,0];含 'ax1' → [0,1];否则 [0,0]。"""

    async def fake(texts):
        out = []
        for t in texts:
            if "ax0" in t:
                out.append([1.0, 0.0])
            elif "ax1" in t:
                out.append([0.0, 1.0])
            else:
                out.append([0.0, 0.0])
        return out

    return fake


@pytest.fixture()
def kb_env(monkeypatch):
    _patch_pg(monkeypatch)
    yield
    # 清理本测试建的数据(按标记前缀)
    import psycopg

    with psycopg.connect(os.environ["KB_TEST_DATABASE_URL"]) as conn:
        conn.execute("DELETE FROM kb_source WHERE updated_by LIKE 'it-%'")
        conn.commit()


async def test_roundtrip_ingest_search_lifecycle(kb_env) -> None:
    run = secrets.token_hex(3)
    faq_id = await store.ingest_source(
        SourceInput(
            title="报名FAQ",
            type="faq",
            question="ax0 怎么报名社团?",
            answer="官网填表。",
            tags=("招新",),
            updated_by=f"it-{run}",
        ),
        embedder=_axis_embedder(),
    )
    doc_id = await store.ingest_source(
        SourceInput(
            title="技术部说明",
            type="doc",
            content_md="# 技术部\nax1 这里写代码。\n## 招新\nax0 九月开放。",
            updated_by=f"it-{run}",
        ),
        embedder=_axis_embedder(),
    )

    # 详情映射
    rec = store.get_source(faq_id)
    assert rec is not None and rec.question.startswith("ax0") and rec.chunk_count == 1
    doc = store.get_source(doc_id)
    assert doc is not None and doc.chunk_count == 2  # 两个标题节各一块

    # 列表
    listed = store.list_sources(keyword="报名FAQ")
    assert listed["total"] >= 1

    # 检索:query 靠近 ax0 轴 → FAQ 排最前(不指定 include_test 不含 test 条目)
    hits = await store.search("ax0 报名", embedder=_axis_embedder(), top_k=3)
    assert hits, "至少命中刚入库的条目"
    assert hits[0].source_id == faq_id
    assert all(h.title for h in hits)

    # 停用立即退出生检索
    assert store.set_source_enabled(faq_id, False, updated_by=f"it-{run}") is True
    hits_after = await store.search("ax0 报名", embedder=_axis_embedder(), top_k=3)
    assert all(h.source_id != faq_id for h in hits_after)
    assert store.set_source_enabled(faq_id, True, updated_by=f"it-{run}") is True

    # kind=test:默认检索不可见,include_test 可见
    test_id = await store.ingest_source(
        SourceInput(
            title="评测条目",
            type="doc",
            content_md="ax0 评测专用内容",
            kind="test",
            updated_by=f"it-{run}",
        ),
        embedder=_axis_embedder(),
    )
    hits_norm = await store.search("ax0 评测专用", embedder=_axis_embedder(), top_k=10)
    assert all(h.source_id != test_id for h in hits_norm)
    hits_test = await store.search(
        "ax0 评测专用", embedder=_axis_embedder(), top_k=10, include_test=True
    )
    assert any(h.source_id == test_id for h in hits_test)

    # 删除级联:chunks 随 source 消失
    assert store.delete_source(test_id) is True
    assert store.get_source(test_id) is None


async def test_reingest_updates_chunks_not_duplicates(kb_env) -> None:
    run = secrets.token_hex(3)
    sid = await store.ingest_source(
        SourceInput(
            title="会被更新的条目",
            type="doc",
            content_md="ax0 第一版",
            updated_by=f"it-{run}",
        ),
        embedder=_axis_embedder(),
    )
    sid2 = await store.ingest_source(
        SourceInput(
            title="会被更新的条目",
            type="doc",
            content_md="ax0 第二版内容变长了\n## 新节\nax1 多了一节",
            updated_by=f"it-{run}",
        ),
        embedder=_axis_embedder(),
        source_id=sid,
    )
    assert sid2 == sid
    rec = store.get_source(sid)
    assert rec is not None and rec.chunk_count == 2  # 重嵌替换,不叠加重份
    import psycopg

    with psycopg.connect(os.environ["KB_TEST_DATABASE_URL"]) as conn:
        n = conn.execute(
            "SELECT count(*) FROM kb_chunks WHERE source_id = %s", (sid,)
        ).fetchone()
        assert n is not None and n[0] == 2


async def test_model_change_bumps_version_and_clears_vectors(kb_env) -> None:
    """换 embedding 模型:旧向量全清+版本 bump+列维度 ALTER(禁混排,#119)。

    评审闸 P1:这是全票风险最高的 DDL 路径(HNSW 索引在列上时 ALTER),
    实测正确性依赖「同事务先 DELETE 再 ALTER」的顺序,必须有真库守护。
    """
    import psycopg

    url = os.environ["KB_TEST_DATABASE_URL"]
    run = secrets.token_hex(3)
    sid = await store.ingest_source(
        SourceInput(
            title="换代前的条目", type="doc", content_md="ax0 旧内容", updated_by=f"it-{run}"
        ),
        embedder=_axis_embedder(),
    )
    hits = await store.search("ax0", embedder=_axis_embedder(), top_k=5)
    assert any(h.source_id == sid for h in hits)

    # 模拟换模型:直接改 meta(制造与配置的 model/dim 不一致)
    with psycopg.connect(url) as conn:
        old_version = conn.execute("SELECT version FROM kb_meta WHERE id = 1").fetchone()[0]
        conn.execute(
            "UPDATE kb_meta SET embed_model = 'it-other-model', dim = 3 WHERE id = 1"
        )
        conn.commit()

    # 下一次写/读触发 ensure:清向量 → 版本 bump → 列 ALTER 回配置维度
    sid2 = await store.ingest_source(
        SourceInput(
            title="换代后的条目", type="doc", content_md="ax0 新内容", updated_by=f"it-{run}"
        ),
        embedder=_axis_embedder(),
    )
    with psycopg.connect(url) as conn:
        meta = conn.execute("SELECT version, dim FROM kb_meta WHERE id = 1").fetchone()
        assert meta[0] == old_version + 1 and meta[1] == DIM
        stale = conn.execute(
            "SELECT count(*) FROM kb_chunks WHERE source_id = %s", (sid,)
        ).fetchone()
        assert stale[0] == 0  # 旧向量已清,绝不与新模型向量混排

    hits2 = await store.search("ax0", embedder=_axis_embedder(), top_k=5)
    assert [h.source_id for h in hits2] == [sid2]  # 只有换代后入库的可检索


async def test_admin_api_roundtrip_real_pg(monkeypatch) -> None:
    """R2 真机等效闸:真实 HTTP(TestClient)→ 鉴权 → store → 真 pgvector 全链。"""
    import contextlib
    from collections.abc import AsyncIterator

    from fastapi.testclient import TestClient

    from official_agent.web import routes
    from official_agent.web.app import create_app

    _patch_pg(monkeypatch)
    monkeypatch.setattr("official_agent.kb.store.embed_texts", _axis_embedder())

    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield object()  # 真值 → /health ok(本测试只关注 KB 面)

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    # lifespan 建表走真实 .env PG(测试外),与 KB 无关 → 打桩
    monkeypatch.setattr(
        "official_agent.state.threads.ensure_agent_threads_table", lambda: None
    )
    monkeypatch.setattr(
        "official_agent.state.conversation.ensure_conversation_table", lambda: None
    )
    monkeypatch.setattr(
        "official_agent.state.config_store.ensure_config_table", lambda: None
    )

    async def _resolve(*_a: object, **_k: object) -> dict:
        return {
            "user_id": 2,
            "role": "admin",
            "role_names": ["管理员"],
            "permission_codes": ["kb:manage"],
            "source": "web",
        }

    monkeypatch.setattr(routes, "resolve", _resolve)

    with TestClient(create_app()) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert (
            client.get("/api/agent/admin/kb/sources").status_code == 401
        )  # 未带 token

        auth = {"Authorization": "Bearer tok"}
        created = client.post(
            "/api/agent/admin/kb/sources",
            headers=auth,
            json={
                "title": "报名FAQ",
                "type": "faq",
                "question": "ax0 怎么报名?",
                "answer": "官网填表",
            },
        )
        assert created.status_code == 201, created.text
        sid = created.json()["source_id"]

        detail = client.get(f"/api/agent/admin/kb/sources/{sid}", headers=auth)
        assert detail.status_code == 200
        assert detail.json()["question"] == "ax0 怎么报名?"
        assert detail.json()["chunk_count"] == 1

        listed = client.get(
            "/api/agent/admin/kb/sources?keyword=报名FAQ", headers=auth
        )
        assert listed.status_code == 200 and listed.json()["total"] >= 1

        # 检索层经真实库确认可召回(HTTP 管理面外圈)
        hits = await store.search("ax0 报名", embedder=_axis_embedder())
        assert any(h.source_id == sid for h in hits)

        assert (
            client.delete(f"/api/agent/admin/kb/sources/{sid}", headers=auth).status_code
            == 200
        )
        assert (
            client.get(f"/api/agent/admin/kb/sources/{sid}", headers=auth).status_code
            == 404
        )
