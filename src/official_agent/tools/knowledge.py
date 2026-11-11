"""知识库检索工具:search_knowledge——客服的社团/部门/FAQ 只读检索。

docstring 契约(ADR-0003):写「何时用」+ 边界,docstring 即模型看到的工具描述。

降级契约:0 命中/服务不可用/EMBED 未配置都不抛异常——返回结构化
状态由模型转述(查不到明说/引导官网),检索故障不阻塞对话。
引用锚是 source_id+title(条目级)。最相关的前两条给完整块(块本身不超过
800 字),其余截短——前两条是作答主依据,截成 200 字常常把答案截掉。

周期过滤:当前开放的招新周期由这里在代码里解析后传给检索,不作为工具参数
暴露给模型——往届的面试时间地点不该因为模型传错周期而被检到。

检索日志:对话里的每次检索落一行 kb_query_log(kb/query_log.py),三种状态都记。
"""

import asyncio
import logging
from typing import Any

from official_agent.kb import query_log as kb_query_log
from official_agent.kb import store as kb_store
from official_agent.kb.embedding import EmbeddingError
from official_agent.observability import turn_trace_id
from official_agent.tools import readonly

logger = logging.getLogger(__name__)

_TOP_K = 4
_FULL_HITS = 2
_SNIPPET_CHARS = 300
#: 读开放周期最多等这么久。正常是命中 60 秒缓存或一次内网请求;后端变慢时
#: (客户端单次超时 15 秒)不能让知识库检索跟着卡住——知识库本身不依赖后端。
_OPEN_CYCLE_TIMEOUT_SECONDS = 3.0

_UNAVAILABLE = {
    "status": "unavailable",
    "message": "知识库检索暂不可用;如实告知用户,并建议查看官网或报名说明,不要编造",
}


async def search_knowledge(query: str) -> dict[str, Any]:
    """查询社团官方知识库(社团介绍/部门职能/招新流程/常见问题)后作答。

    何时用:用户问「社团有哪些部门/某部门做什么/怎么加入/面试流程/
    协会活动/报名常见问题」等社团公开信息时,先查这里再回答;
    回答时注明信息来自哪条条目(用 title,如「据知识库《技术部介绍》」)。

    边界:只覆盖管理面板录入的官方知识;返回 status=not_found 时明确说
    「知识库里没有这部分,建议查看官网或报名说明」,绝不凭常识编造社团
    细节;status=unavailable 时如实说明检索暂不可用并引导官网。
    这里没有候选人个人数据(个人进度/面试安排走其他工具)。

    query 要写成**不依赖上下文的完整问题**:用户追问「那它呢」「这个要多久」时,
    把指代换成具体对象再查(如「媒体部的面试要多久」),否则检索不到。

    示例:search_knowledge(query="技术部主要负责什么")
    """
    cycle_ids = await _open_cycle_ids()
    try:
        outcome = await kb_store.search_with_signals(query, top_k=_TOP_K, cycle_ids=cycle_ids)
    except EmbeddingError as exc:
        logger.warning("kb_search embedding 不可用,降级 unavailable error=%s", exc)
        _record_query(query, kb_query_log.STATUS_UNAVAILABLE, cycle_ids)
        return dict(_UNAVAILABLE)
    except Exception:  # noqa: BLE001 — 检索失败同降级,不阻塞对话
        logger.warning("kb_search 检索失败,降级 unavailable", exc_info=True)
        _record_query(query, kb_query_log.STATUS_UNAVAILABLE, cycle_ids)
        return dict(_UNAVAILABLE)
    hits = outcome.hits
    _record_query(
        query,
        kb_query_log.STATUS_OK if hits else kb_query_log.STATUS_NOT_FOUND,
        cycle_ids,
        outcome,
    )
    if not hits:
        return {
            "status": "not_found",
            "message": "知识库中没有相关内容;明确告知用户并建议查看官网/报名说明",
            "results": [],
        }
    return {
        "status": "ok",
        "results": [
            {
                "source_id": h.source_id,
                "title": h.title,
                "heading": h.heading_path or None,
                "snippet": h.chunk_text if i < _FULL_HITS else h.chunk_text[:_SNIPPET_CHARS],
            }
            for i, h in enumerate(hits)
        ],
    }


async def _open_cycle_ids() -> tuple[int, ...]:
    """当前开放的招新周期 id(可能同时开着多个);读不到或超时返回空元组。"""
    try:
        cycles = await asyncio.wait_for(
            readonly.get_open_cycle(), timeout=_OPEN_CYCLE_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001
        # fail-open 到「只查常青条目」:本届的时间地点这一轮查不到,模型按
        # not_found 如实说没有;反过来放开过滤,可能拿往届信息作答,比查不到更糟
        logger.warning("kb_search 开放周期读取失败,本次只检索常青条目", exc_info=True)
        return ()
    if not isinstance(cycles, list):
        return ()
    return tuple(
        c["cycleId"] for c in cycles if isinstance(c, dict) and isinstance(c.get("cycleId"), int)
    )


def _conversation_thread_id() -> str:
    """当前工具调用所在会话的 thread_id;不在对话图里(MCP/脚本直调)返回空串。"""
    from langgraph.config import get_config

    try:
        config = get_config()
    except RuntimeError:  # 不在 runnable 上下文里
        return ""
    return str((config.get("configurable") or {}).get("thread_id") or "")


def _record_query(
    query: str,
    status: str,
    cycle_ids: tuple[int, ...],
    outcome: kb_store.SearchOutcome | None = None,
) -> None:
    """把这次检索记进检索日志(后台写,不等待)。outcome 缺省表示检索本身失败。

    只记对话里的检索:日志要回答的是「真实用户问了什么没答上」,开发者经 MCP
    试查会污染它;而且每行必须能随会话一起删除,没有 thread_id 的行删不掉。
    """
    thread_id = _conversation_thread_id()
    if not thread_id:
        return
    hits = outcome.hits if outcome else []
    kb_query_log.record_query(
        kb_query_log.QueryLogEntry(
            thread_id=thread_id,
            trace_id=turn_trace_id(),
            query=query,
            status=status,
            best_vec_score=outcome.best_vec_score if outcome else None,
            best_kw_ratio=outcome.best_kw_ratio if outcome else None,
            # 同一条目的多个块只记一次,保持命中顺序
            hit_source_ids=tuple(dict.fromkeys(h.source_id for h in hits)),
            cycle_ids=cycle_ids,
        )
    )
