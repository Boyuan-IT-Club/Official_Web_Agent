"""对话观测面:conversation_log 落账、KB 引用锚收集、usage / 前缀指纹取样。

web 入口不顶层依赖 psycopg(无 PG 环境也要能跑服务):本模块对
state.conversation 的引用一律函数内 lazy import——观测侧不给主链路加
硬依赖,写入失败 fail-open(ADR-0005)。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from official_agent.web.session_store import SessionState

logger = logging.getLogger(__name__)

# fire-and-forget 观测写入任务的强引用集:事件循环只持弱引用,不留在集里的
# 任务可能在完成前被 GC 中途取消(asyncio 官方文档明确告诫);完成后经回调出集。
_background_tasks: set[asyncio.Task[None]] = set()


def log_conversation(
    session: SessionState,
    *,
    user_message: str,
    reply_summary: str,
    tools: list[str],
    duration_ms: int,
    error_code: str | None = None,
    usage: dict[str, int | None] | None = None,
    prefix_hash: str | None = None,
    compress_event: str | None = None,
) -> None:
    """落一行 conversation_log(fail-open,非阻塞)。

    fire-and-forget:psycopg 是同步驱动,写入经 to_thread 进线程池,
    不占事件循环、不阻塞 SSE 流尾(ADR-0005 fail-open)。
    """
    from official_agent.state.conversation import write_conversation

    async def _write() -> None:
        try:
            await asyncio.to_thread(
                write_conversation,
                thread_id=session.session_id,
                user_id=session.identity.get("user_id"),
                channel=session.identity.get("source") or "web",
                user_message=user_message,
                reply_summary=reply_summary,
                tools=tools,
                duration_ms=duration_ms,
                error_code=error_code,
                prefix_hash=prefix_hash,
                compress_event=compress_event,
                **(usage or {}),
            )
        except Exception:  # noqa: BLE001 — 观测写入失败不拖垮对话(ADR-0005)
            logger.warning("conversation_log 写入失败(已忽略)", exc_info=True)

    task = asyncio.create_task(_write())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def extract_usage(usage_metadata: dict[str, Any] | None) -> dict[str, int | None]:
    """从 LLM usage_metadata 提取 token 数(lazy 转发,供 patch)。"""
    from official_agent.state.conversation import extract_usage as _impl

    return _impl(usage_metadata)


def prefix_hash(system_prompt: str, tool_names: list[str]) -> str:
    """缓存前缀稳定性 hash(lazy 转发,供 patch)。"""
    from official_agent.state.conversation import prefix_hash as _impl

    return _impl(system_prompt, tool_names)


def collect_sources(tool_msg: ToolMessage, sources: list, seen: set) -> None:
    """search_knowledge 的 ToolMessage → 轮级引用锚([n]→条目,保序去重)。

    内容是工具返回的 JSON;解析失败只丢引用,不影响主回复(降级纪律)。
    """
    try:
        # 流式 updates 里会出现空 content 的占位 ToolMessage,跳过不作失败
        if not tool_msg.content:
            return
        data = (
            json.loads(tool_msg.content)
            if isinstance(tool_msg.content, str)
            else tool_msg.content
        )
        for r in (data or {}).get("results") or []:
            sid = r.get("source_id")
            if sid and sid not in seen:
                seen.add(sid)
                sources.append({"source_id": sid, "title": r.get("title", "")})
    except Exception:  # noqa: BLE001 — 引用属增强面,失败不拖垮对话
        logger.warning("search_knowledge sources 收集失败(已忽略)", exc_info=True)
