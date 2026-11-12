"""search_knowledge 工具 + 角色装配测试。

- 工具行为:ok 结构/0 命中/服务不可用全部**不抛异常**(降级契约)
- 周期过滤:开放周期在代码里解析后传给检索;读不到时只查常青条目
- 检索日志:对话里的检索三种状态都记;不在对话里(没有 thread_id)不记
- 装配:candidate/member/admin 给 search_knowledge;unknown(访客)不给
"""

import pytest

from official_agent.graphs.assistant import assemble_tools
from official_agent.kb.embedding import EmbeddingError
from official_agent.kb.query_log import QueryLogEntry
from official_agent.kb.store import SearchHit, SearchOutcome
from official_agent.tools import knowledge

#: 真实的周期解析函数(autouse 替身会盖掉模块属性,测它本身时用这个引用)
_real_open_cycle_ids = knowledge._open_cycle_ids


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


def _stub_search(monkeypatch, outcome: SearchOutcome | Exception) -> dict:
    """把检索换成固定结果(或固定异常);返回的 dict 记下调用入参。"""
    seen: dict = {}

    async def _search(query, **kw):
        seen.update(query=query, **kw)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(knowledge.kb_store, "search_with_signals", _search)
    return seen


@pytest.fixture(autouse=True)
def _no_open_cycle(monkeypatch):
    """默认没有开放周期(也就不碰后端);要测周期的用例自行覆盖。"""

    async def _none() -> tuple[int, ...]:
        return ()

    monkeypatch.setattr(knowledge, "_open_cycle_ids", _none)


@pytest.fixture
def recorded(monkeypatch) -> list[QueryLogEntry]:
    """让工具以为自己在会话 web:u1:t1 里,并截下它要写的检索日志行。"""
    entries: list[QueryLogEntry] = []
    monkeypatch.setattr(knowledge, "_conversation_thread_id", lambda: "web:u1:t1")
    monkeypatch.setattr(knowledge, "turn_trace_id", lambda: "ab" * 16)
    monkeypatch.setattr(knowledge.kb_query_log, "record_query", entries.append)
    return entries


async def test_search_knowledge_ok_maps_hits(monkeypatch) -> None:
    _stub_search(monkeypatch, SearchOutcome(hits=[_hit()]))
    result = await knowledge.search_knowledge("技术部做什么")
    assert result["status"] == "ok"
    r = result["results"][0]
    assert r["source_id"] == "kb_a1"
    assert r["title"] == "技术部介绍"
    assert r["heading"] == "技术部 > 职责"
    assert r["snippet"] == _hit().chunk_text  # 前两条给完整块


async def test_search_knowledge_truncates_lower_ranked_hits(monkeypatch) -> None:
    hits = [_hit(source_id=f"kb_{i}") for i in range(4)]
    _stub_search(monkeypatch, SearchOutcome(hits=hits))
    result = await knowledge.search_knowledge("技术部做什么")
    lengths = [len(r["snippet"]) for r in result["results"]]
    assert lengths[0] == lengths[1] == len(_hit().chunk_text)
    assert lengths[2] <= 300 and lengths[3] <= 300


async def test_search_knowledge_not_found(monkeypatch) -> None:
    _stub_search(monkeypatch, SearchOutcome(hits=[]))
    result = await knowledge.search_knowledge("不存在的主题")
    assert result["status"] == "not_found"
    assert result["results"] == []
    assert "官网" in result["message"]  # 降级话术指引


async def test_search_knowledge_embedding_error_degrades(monkeypatch) -> None:
    """EMBED 未配置/端点失败 → unavailable,不抛异常外层(降级契约)。"""
    _stub_search(monkeypatch, EmbeddingError("embedding 未配置"))
    result = await knowledge.search_knowledge("任何")
    assert result["status"] == "unavailable"


async def test_search_knowledge_generic_error_degrades(monkeypatch) -> None:
    _stub_search(monkeypatch, RuntimeError("db down"))
    result = await knowledge.search_knowledge("任何")
    assert result["status"] == "unavailable"


async def test_search_knowledge_passes_topk(monkeypatch) -> None:
    seen = _stub_search(monkeypatch, SearchOutcome(hits=[]))
    await knowledge.search_knowledge("招新流程")
    assert seen["query"] == "招新流程"
    assert seen["top_k"] == knowledge._TOP_K


# ── 周期过滤 ────────────────────────────────────────────


async def test_search_knowledge_passes_open_cycles(monkeypatch) -> None:
    async def _open() -> tuple[int, ...]:
        return (12, 13)

    monkeypatch.setattr(knowledge, "_open_cycle_ids", _open)
    seen = _stub_search(monkeypatch, SearchOutcome(hits=[]))
    await knowledge.search_knowledge("面试在哪")
    assert seen["cycle_ids"] == (12, 13)


async def test_open_cycle_ids_reads_every_open_cycle(monkeypatch) -> None:
    async def _cycles():
        return [{"cycleId": 12, "cycleName": "秋招"}, {"cycleId": 13}, {"cycleName": "缺 id"}]

    monkeypatch.setattr(knowledge.readonly, "get_open_cycle", _cycles)
    assert await _real_open_cycle_ids() == (12, 13)


async def test_open_cycle_ids_falls_back_to_evergreen_on_backend_failure(monkeypatch) -> None:
    """后端读不到开放周期:返回空(只查常青),绝不放开过滤去检往届条目。"""

    async def _down():
        raise RuntimeError("backend down")

    monkeypatch.setattr(knowledge.readonly, "get_open_cycle", _down)
    assert await _real_open_cycle_ids() == ()


async def test_open_cycle_ids_does_not_wait_for_a_slow_backend(monkeypatch) -> None:
    """后端变慢时不让知识库检索跟着卡住:超过上限按读不到处理。"""
    import asyncio

    async def _slow():
        await asyncio.sleep(30)
        return [{"cycleId": 12}]

    monkeypatch.setattr(knowledge.readonly, "get_open_cycle", _slow)
    monkeypatch.setattr(knowledge, "_OPEN_CYCLE_TIMEOUT_SECONDS", 0.01)
    assert await _real_open_cycle_ids() == ()


async def test_open_cycle_ids_ignores_unexpected_shape(monkeypatch) -> None:
    async def _weird():
        return {"message": "no open cycle"}

    monkeypatch.setattr(knowledge.readonly, "get_open_cycle", _weird)
    assert await _real_open_cycle_ids() == ()


# ── 检索日志 ────────────────────────────────────────────


async def test_hit_is_recorded_with_scores_and_sources(monkeypatch, recorded) -> None:
    async def _open() -> tuple[int, ...]:
        return (12,)

    monkeypatch.setattr(knowledge, "_open_cycle_ids", _open)
    hits = [_hit(source_id="kb_a"), _hit(source_id="kb_a"), _hit(source_id="kb_b")]
    _stub_search(monkeypatch, SearchOutcome(hits=hits, best_vec_score=0.91, best_kw_ratio=0.5))
    await knowledge.search_knowledge("技术部做什么")
    assert recorded == [
        QueryLogEntry(
            thread_id="web:u1:t1",
            trace_id="ab" * 16,
            query="技术部做什么",
            status="ok",
            best_vec_score=0.91,
            best_kw_ratio=0.5,
            hit_source_ids=("kb_a", "kb_b"),  # 同一条目的多个块只记一次
            cycle_ids=(12,),
        )
    ]


async def test_miss_is_recorded_with_pre_threshold_scores(monkeypatch, recorded) -> None:
    """没命中时也带着过门槛之前的最高分——这是这张表存在的理由。"""
    _stub_search(monkeypatch, SearchOutcome(hits=[], best_vec_score=0.42, best_kw_ratio=0.0))
    await knowledge.search_knowledge("计拔怎么报名")
    (entry,) = recorded
    assert entry.status == "not_found"
    assert entry.best_vec_score == 0.42 and entry.hit_source_ids == ()


@pytest.mark.parametrize("error", [EmbeddingError("embedding 未配置"), RuntimeError("db down")])
async def test_failure_is_recorded_as_unavailable(monkeypatch, recorded, error) -> None:
    _stub_search(monkeypatch, error)
    result = await knowledge.search_knowledge("任何")
    assert result["status"] == "unavailable"
    (entry,) = recorded
    assert entry.status == "unavailable"
    assert entry.best_vec_score is None and entry.best_kw_ratio is None


async def test_search_outside_conversation_is_not_recorded(monkeypatch) -> None:
    """MCP/脚本直调没有会话:不记(记了也无法随会话删除)。"""
    entries: list[QueryLogEntry] = []
    monkeypatch.setattr(knowledge.kb_query_log, "record_query", entries.append)
    _stub_search(monkeypatch, SearchOutcome(hits=[_hit()]))
    await knowledge.search_knowledge("技术部做什么")  # 测试进程不在 runnable 上下文里
    assert entries == []


async def test_thread_id_comes_from_the_running_graph_config() -> None:
    from langchain_core.runnables import RunnableLambda

    async def _probe(_: object) -> str:
        return knowledge._conversation_thread_id()

    got = await RunnableLambda(_probe).ainvoke(
        None, config={"configurable": {"thread_id": "web:u7:deadbeef"}}
    )
    assert got == "web:u7:deadbeef"
    assert knowledge._conversation_thread_id() == ""  # 图外


# ── 角色装配(访客不给) ──────────────────────────────


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


async def test_public_open_cycle_ids_matches_the_tool_filter(monkeypatch) -> None:
    """离线流程读开放周期与在线检索是同一个口径(含读不到时退到只查常青)。"""

    async def _open() -> tuple[int, ...]:
        return (12, 13)

    monkeypatch.setattr(knowledge, "_open_cycle_ids", _open)
    assert await knowledge.open_cycle_ids() == (12, 13)
