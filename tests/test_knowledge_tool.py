"""RAG #134 R3:search_knowledge 工具 + 角色装配测试。

- 工具行为:ok 结构/0 命中/服务不可用全部**不抛异常**(#120 降级契约)
- 装配(#120):candidate/member/admin 给 search_knowledge;unknown(访客)不给
"""

from unittest.mock import patch

import pytest

from official_agent.graphs.assistant import assemble_tools
from official_agent.kb.embedding import EmbeddingError
from official_agent.kb.store import SearchHit
from official_agent.tools import knowledge


def _hit(**over) -> SearchHit:
    base = {
        "source_id": "kb_a1",
        "title": "技术部介绍",
        "chunk_text": "技术部负责官网与工具开发。" * 30,  # 超过截断长度
        "heading_path": "技术部 > 职责",
        "score": 0.91,
    }
    base.update(over)
    return SearchHit(**base)


async def test_search_knowledge_ok_maps_hits() -> None:
    with patch.object(knowledge.kb_store, "search", return_value=[_hit()]):
        result = await knowledge.search_knowledge("技术部做什么")
    assert result["status"] == "ok"
    r = result["results"][0]
    assert r["source_id"] == "kb_a1"
    assert r["title"] == "技术部介绍"
    assert r["heading"] == "技术部 > 职责"
    assert len(r["snippet"]) <= 200  # 块截断


async def test_search_knowledge_not_found(monkeypatch) -> None:
    async def _empty(query, **kw):
        return []

    monkeypatch.setattr(knowledge.kb_store, "search", _empty)
    result = await knowledge.search_knowledge("不存在的主题")
    assert result["status"] == "not_found"
    assert result["results"] == []
    assert "官网" in result["message"]  # 降级话术指引


async def test_search_knowledge_embedding_error_degrades(monkeypatch) -> None:
    """EMBED 未配置/端点失败 → unavailable,不抛异常外层(#120)。"""

    async def _boom(query, **kw):
        raise EmbeddingError("embedding 未配置")

    monkeypatch.setattr(knowledge.kb_store, "search", _boom)
    result = await knowledge.search_knowledge("任何")
    assert result["status"] == "unavailable"


async def test_search_knowledge_generic_error_degrades(monkeypatch) -> None:
    async def _boom(query, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(knowledge.kb_store, "search", _boom)
    result = await knowledge.search_knowledge("任何")
    assert result["status"] == "unavailable"


async def test_search_knowledge_passes_topk(monkeypatch) -> None:
    seen: dict = {}

    async def _spy(query, **kw):
        seen.update(query=query, **kw)
        return []

    monkeypatch.setattr(knowledge.kb_store, "search", _spy)
    await knowledge.search_knowledge("招新流程")
    assert seen["query"] == "招新流程"
    assert seen["top_k"] == knowledge._TOP_K


# ── 角色装配(#120:访客不给) ──────────────────────────────


@pytest.mark.parametrize("role", ["admin", "member", "candidate"])
def test_search_knowledge_assembled_for_known_roles(role: str) -> None:
    tools = assemble_tools({"role": role}, user_token="tok")
    assert any(getattr(t, "__name__", "") == "search_knowledge" for t in tools), (
        f"{role} 应装配 search_knowledge"
    )


def test_search_knowledge_not_assembled_for_unknown() -> None:
    tools = assemble_tools({"role": "unknown"}, user_token="")
    assert not any(getattr(t, "__name__", "") == "search_knowledge" for t in tools)
    assert tools == []  # unknown 本就空集(装配层第一道闸)


def test_role_table_file_level() -> None:
    """spec:角色表文件级断言——三档含 kb 工具,unknown 不含。"""
    from official_agent.graphs.assistant import _ALL_TOOLS, _ROLE_TOOL_NAMES

    assert "search_knowledge" in _ALL_TOOLS
    for role in ("admin", "member", "candidate"):
        assert "search_knowledge" in _ROLE_TOOL_NAMES[role]
    assert "search_knowledge" not in _ROLE_TOOL_NAMES["unknown"]
