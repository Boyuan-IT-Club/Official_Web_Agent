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

    from official_agent.config import get_settings  # noqa: F401

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
