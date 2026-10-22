"""会话压缩:超阈值后摘要回写,注入侧只喂「摘要 + 近几轮」。

策略:感知查询意图的批量摘要
(ContextAware + citations)+ tiktoken 强计数,temperature 0(reasoning-safe:
确定性输出,不引入创造性漂移)。摘要模型由 model_compress 配置,缺省同主模型。

滚动摘要:第二次及以后的压缩,上一份摘要不再当成一条普通旧消息重新概括
(每压一次就再损失一层细节),而是作为「已有摘要」单独交给摘要器,只把新增
的旧消息并进去。摘要固定带「关键事实」段,周期/简历/场次编号等原样保留。

摘要消息的身份:以 HumanMessage 承载(部分模型端点只接受首条 system),但带
history_summary 标记——该标记只存 checkpoint、不发给模型;历史回看据此跳过,
不把摘要当成用户自己说的话展示出来。

回写与全量可回溯:压缩结果经 update_state 写成 checkpoint 新版本
(先 REMOVE_ALL_MESSAGES 再加「摘要 + 近几轮」);PostgresSaver 不删
旧版本行,全量历史仍可 get_state_history 回溯——checkpointer 始终是
对话原文权威源。

fail-open(ADR-0005):压缩任何一步失败由调用方吞掉,不影响当轮对话。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from official_agent.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

# 默认阈值(运维入口在 config.py Settings 同名 .env 键;此处供独立调用兜底,
# 两处保持一致)
DEFAULT_THRESHOLD_TOKENS = 24_000
DEFAULT_RECENT_KEEP = 12
# 摘要输出预算(reasoning-safe:摘要足够装下固定保留清单即可,防发散)
SUMMARY_MAX_TOKENS = 1024
# 压缩熔断器(ADR-0004):连续 N 次失败即停,降级并告警(人工从日志发现)
_MAX_CONSECUTIVE_FAILURES = 3

#: 摘要消息的标记键(additional_kwargs 里):回看投影与滚动摘要靠它识别
SUMMARY_FLAG = "history_summary"
#: 摘要消息正文前缀:告诉模型这是系统生成的摘要,不是用户本轮的话
SUMMARY_PREFIX = "[历史摘要:系统根据较早的对话生成,不是用户本轮的发言]"
#: 旧版摘要前缀(标记出现之前写进 checkpoint 的摘要),滚动时同样识别
_LEGACY_PREFIX = "[历史摘要]"

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
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


def count_tokens(messages: Sequence[BaseMessage]) -> int:
    """tiktoken 强计数(cl100k_base);编码不可用时 1 字 ≈ 1 token 保守高估
    (偏高只导致提前压缩,不丢上下文,方向安全)。"""
    try:
        enc = _encoding()
    except Exception:  # noqa: BLE001 — 无网络/编码缺失时保守降级
        return sum(max(1, len(_content_text(m))) for m in messages)
    return sum(len(enc.encode(_content_text(m), disallowed_special=())) for m in messages)


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


def is_summary_message(message: BaseMessage) -> bool:
    """是否压缩产出的摘要消息(含标记出现之前的旧版摘要)。"""
    if (getattr(message, "additional_kwargs", None) or {}).get(SUMMARY_FLAG):
        return True
    return message.type == "human" and _content_text(message).startswith(_LEGACY_PREFIX)


def _summary_body(message: BaseMessage) -> str:
    text = _content_text(message)
    for prefix in (SUMMARY_PREFIX, _LEGACY_PREFIX):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text.strip()


def summary_message(summary: str) -> HumanMessage:
    """包装摘要正文为带标记的消息(压缩后序列的首条)。"""
    return HumanMessage(
        content=f"{SUMMARY_PREFIX}\n{summary}", additional_kwargs={SUMMARY_FLAG: True}
    )


async def summarize_messages(older: Sequence[BaseMessage], query: str, model: Any) -> HumanMessage:
    """生成摘要消息:感知当前查询意图(ContextAware)+ [T#] 引用标记。

    引用编号按传入顺序 T1..Tn 对应**新增**旧消息,便于排查时回溯原文
    (checkpointer 全量可查)。older 首条若是上一份摘要,作为「已有摘要」单独
    交给摘要器合并,不参与编号(滚动摘要,见模块说明)。prompt 来自
    prompts/compression.md(ADR-0004 版本化;{query}/{previous}/{numbered}
    占位符由代码填充)。
    """
    previous = ""
    rest = list(older)
    if rest and is_summary_message(rest[0]):
        previous = _summary_body(rest[0])
        rest = rest[1:]
    numbered = "\n".join(
        f"[T{i}] {type(m).__name__}: {_content_text(m)}" for i, m in enumerate(rest, start=1)
    )
    prompt = load_prompt("compression.md").format(
        query=query or "未提供", previous=previous or "(无,这是第一次压缩)", numbered=numbered
    )
    response = await model.ainvoke(prompt)
    return summary_message(_content_text(response).strip())


# ── 压缩熔断器(ADR-0004:连续 N 次压缩失败即停,降级并告警) ─────────────

_fail_streak = 0


def compression_paused() -> bool:
    """连续失败达阈值后暂停压缩尝试(进程级;重启或成功后恢复)。"""
    return _fail_streak >= _MAX_CONSECUTIVE_FAILURES


def record_compression_success() -> None:
    global _fail_streak
    _fail_streak = 0


def record_compression_failure() -> None:
    global _fail_streak
    _fail_streak += 1
    if compression_paused():
        logger.warning(
            "会话压缩连续失败 %d 次,熔断暂停(降级为不压缩;排查后重启进程恢复)",
            _fail_streak,
        )
