"""M6 #114 会话压缩:超阈值后摘要回写,注入侧只喂「摘要 + 近几轮」。

策略(决策 #108,grill 定拍 2026-09-04):感知查询意图的批量摘要
(ContextAware + citations)+ tiktoken 强计数,摘要走 model_light、
temperature 0(reasoning-safe:确定性输出,不引入创造性漂移)。

回写与全量可回溯:压缩结果经 update_state 写成 checkpoint 新版本
(先 REMOVE_ALL_MESSAGES 再加「摘要 + 近几轮」);PostgresSaver 不删
旧版本行,全量历史仍可 get_state_history 回溯——checkpointer 始终是
对话原文权威源(#102)。

fail-open(ADR-0005):压缩任何一步失败由调用方吞掉,不影响当轮对话。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

# 默认阈值(可被 Settings 同名配置覆盖):token 上限 / 保留近几轮条数
DEFAULT_THRESHOLD_TOKENS = 24_000
DEFAULT_RECENT_KEEP = 12

_ENCODING: Any = None


def _encoding() -> Any:
    global _ENCODING
    if _ENCODING is None:
        import tiktoken

        # cl100k:o200k 对中文压缩过狠(约 0.5 token/字)会低估用量推迟压缩;
        # cl100k 约 0.6-1 token/字,更接近 Anthropic/DeepSeek 真实水平,方向安全
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def _content_text(message: BaseMessage) -> str:
    """消息文本(多模态 content 取文本块拼接,仅用于计数/摘要)。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content)


def count_tokens(messages: Sequence[BaseMessage]) -> int:
    """tiktoken 强计数(o200k_base);编码不可用时 1 字 ≈ 1 token 保守高估
    (偏高只导致提前压缩,不丢上下文,方向安全)。"""
    try:
        enc = _encoding()
    except Exception:  # noqa: BLE001 — 无网络/编码缺失时保守降级
        return sum(max(1, len(_content_text(m))) for m in messages)
    return sum(
        len(enc.encode(_content_text(m), disallowed_special=())) for m in messages
    )


def safe_split(
    messages: Sequence[BaseMessage], recent_keep: int
) -> tuple[list[BaseMessage], list[BaseMessage]] | None:
    """切 (older, recent)。切点从 len-recent_keep 向前回退,保证不拆散
    AI(tool_calls) 与 Tool 结果对;退无可退(无安全切点)→ None。"""
    if len(messages) <= recent_keep or recent_keep < 1:
        return None
    cut = len(messages) - recent_keep
    while cut > 0:
        prev, cur = messages[cut - 1], messages[cut]
        if getattr(prev, "tool_calls", None) or cur.type == "tool":
            cut -= 1
            continue
        return list(messages[:cut]), list(messages[cut:])
    return None


@dataclass
class CompressionResult:
    """压缩产出:新消息序列 + 事件留痕元数据(进 conversation_log)。"""

    new_messages: list[BaseMessage]
    trigger_tokens: int
    covered: int  # 被摘要覆盖的原始消息条数
    summary_tokens: int


async def maybe_compress(
    messages: Sequence[BaseMessage],
    *,
    summarize_fn: Callable[[list[BaseMessage], str], Awaitable[HumanMessage]],
    threshold: int = DEFAULT_THRESHOLD_TOKENS,
    recent_keep: int = DEFAULT_RECENT_KEEP,
    query: str = "",
) -> CompressionResult | None:
    """超阈值 → 摘要 older 并与 recent 拼成新序列;否则 / 不可切 → None。"""
    trigger_tokens = count_tokens(messages)
    if trigger_tokens <= threshold:
        return None
    split = safe_split(messages, recent_keep)
    if split is None:
        return None
    older, recent = split
    summary_message = await summarize_fn(older, query)
    new_messages = [summary_message, *recent]
    return CompressionResult(
        new_messages=new_messages,
        trigger_tokens=trigger_tokens,
        covered=len(older),
        summary_tokens=count_tokens(new_messages),
    )


async def summarize_messages(
    older: Sequence[BaseMessage], query: str, model: Any
) -> HumanMessage:
    """生成摘要消息:感知当前查询意图(ContextAware)+ [T#] 引用标记。

    引用编号按传入顺序 T1..Tn 对应 older 消息,便于排查时回溯原文
    (checkpointer 全量可查)。输出为 [历史摘要] 前缀的 HumanMessage,
    作为压缩后序列首条注入。
    """
    numbered = "\n".join(
        f"[T{i}] {type(m).__name__}: {_content_text(m)}"
        for i, m in enumerate(older, start=1)
    )
    prompt = (
        "你是客服会话的压缩器。把下面的旧消息压缩成一份摘要,供后续对话继续使用。\n"
        "要求:\n"
        "- 只保留对后续对话可能有用的信息:用户身份相关事实、已做过的查询与结论、未决问题\n"
        "- 每条要点末尾标注来源编号,如 [T1]、[T3],不编造、不遗漏未回答的问题\n"
        f"- 结合当前用户意图判断什么信息相关:{query if query else '未提供'}\n\n"
        f"旧消息(逐条编号):\n{numbered}\n\n"
        "输出:纯文本中文摘要。"
    )
    response = await model.ainvoke(prompt)
    summary = _content_text(response).strip()
    return HumanMessage(content=f"[历史摘要]\n{summary}")
