"""M6 #114 会话压缩单测(#109 预定接缝:token 计数/摘要回写形状/触发阈值)。

策略(决策 #108,grill 定拍):感知查询意图的批量摘要(ContextAware +
citations)+ tiktoken 强计数。外部行为契约:
- count_tokens:中英文都计入,空序列为 0
- safe_split:切点永不拆散 tool_call / tool 结果对;无法安全切 → None
- maybe_compress:阈值内不动;超阈值产出「摘要 + 近几轮」新序列与事件元数据
- summarize_messages:prompt 带编号旧消息与当前意图;产出带 [历史摘要] 与 [T#] 引用
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from official_agent.graphs.assistant.compression import (
    count_tokens,
    maybe_compress,
    safe_split,
    summarize_messages,
)


def _tool_pair(tag: str) -> list:
    """一组完整的工具调用对(AI tool_calls + Tool 结果)。"""
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": f"t_{tag}", "args": {}, "id": f"call_{tag}"}],
        ),
        ToolMessage(content=f"结果_{tag}", tool_call_id=f"call_{tag}"),
    ]


def _pairs_intact(older: list, recent: list) -> bool:
    """任一 tool_call 的调用方与结果必须落在同一侧。"""
    calls_older = {tc["id"] for m in older for tc in (getattr(m, "tool_calls", None) or [])}
    results_older = {m.tool_call_id for m in older if m.type == "tool"}
    calls_recent = {tc["id"] for m in recent for tc in (getattr(m, "tool_calls", None) or [])}
    results_recent = {m.tool_call_id for m in recent if m.type == "tool"}
    return not (calls_older & results_recent) and not (calls_recent & results_older)


# ── token 计数 ───────────────────────────────────────────────────────────


def test_count_tokens_counts_cjk_and_ascii() -> None:
    assert count_tokens([]) == 0
    msgs = [HumanMessage(content="你好,世界"), AIMessage(content="hello world")]
    assert count_tokens(msgs) >= 7  # cl100k:CJK≈0.7/字 + ASCII 词级


def test_count_tokens_grows_with_content() -> None:
    short = count_tokens([HumanMessage(content="hi")])
    long = count_tokens([HumanMessage(content="hi " * 50)])
    assert long > short * 5


# ── 安全切分 ─────────────────────────────────────────────────────────────


def test_safe_split_respects_recent_keep() -> None:
    msgs = [
        m
        for i in range(3)
        for m in (HumanMessage(content=f"q{i}"), AIMessage(content=f"a{i}"))
    ] + _tool_pair("x")
    older, recent = safe_split(msgs, recent_keep=2)
    assert recent == msgs[-2:]
    assert older == msgs[:-2]


def test_safe_split_never_breaks_tool_pair() -> None:
    msgs = [HumanMessage(content="q1"), AIMessage(content="a1")]
    msgs += _tool_pair("p1") + _tool_pair("p2") + _tool_pair("p3")
    # 让朴素切点恰好落在 AI(tool_calls) 与 Tool 之间,recent_keep 逐个取值验证
    for keep in range(1, len(msgs)):
        split = safe_split(msgs, recent_keep=keep)
        if split is None:
            continue
        older, recent = split
        assert _pairs_intact(older, recent), f"keep={keep} 拆散了工具对"
        assert older + recent == msgs


def test_safe_split_all_tool_chain_too_short_returns_none() -> None:
    # 整条只剩一个完整工具对、还要保 1 条近轮 → 无安全切点
    msgs = _tool_pair("only")
    assert safe_split(msgs, recent_keep=1) is None


# ── 触发与回写形状 ────────────────────────────────────────────────────────


async def _fake_summarize(older: list, query: str) -> HumanMessage:
    return HumanMessage(content=f"[历史摘要] 共 {len(older)} 条,意图:{query}")


async def test_maybe_compress_under_threshold_returns_none() -> None:
    msgs = [HumanMessage(content="短问题"), AIMessage(content="短回答")]
    assert (
        await maybe_compress(
            msgs, summarize_fn=_fake_summarize, threshold=10_000, recent_keep=2
        )
        is None
    )


async def test_maybe_compress_over_threshold_returns_summary_plus_recent() -> None:
    msgs = [HumanMessage(content="问题" * 30), AIMessage(content="回答" * 30)] * 10
    threshold = count_tokens(msgs) - 1  # 刚好压线触发
    result = await maybe_compress(
        msgs, summarize_fn=_fake_summarize, threshold=threshold, recent_keep=4
    )
    assert result is not None
    assert result.trigger_tokens > threshold
    # 回写形状:首条为 [历史摘要] 摘要消息,后接最近 4 条原始消息
    assert result.new_messages[0].content.startswith("[历史摘要]")
    assert result.new_messages[1:] == msgs[-4:]
    assert result.covered == len(msgs) - 4
    assert result.summary_tokens > 0


async def test_maybe_compress_unsafe_split_returns_none() -> None:
    # 超阈值但无法安全切:整条只有一个不可拆的工具对 → 本轮不压缩
    msgs = _tool_pair("only")
    total = count_tokens(msgs)
    assert (
        await maybe_compress(
            msgs, summarize_fn=_fake_summarize, threshold=total - 1, recent_keep=1
        )
        is None
    )


# ── 摘要器(ContextAware + citations) ────────────────────────────────────


class _FakeModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: str) -> AIMessage:
        self.prompts.append(prompt)
        return AIMessage(content="用户问过面试安排 [T1];已答复周期信息 [T2]")


async def test_summarize_messages_prompt_intent_and_citations() -> None:
    model = _FakeModel()
    older = [HumanMessage(content="我的面试安排是什么"), AIMessage(content="已查到安排")]
    result = await summarize_messages(older, query="面试结果怎么算", model=model)
    prompt = model.prompts[0]
    assert "[T1]" in prompt and "[T2]" in prompt  # 逐条编号
    assert "面试结果怎么算" in prompt  # 感知当前查询意图
    assert "我的面试安排是什么" in prompt  # 原文进 prompt
    # 回写消息:摘要 + 保留引用标记
    assert isinstance(result, HumanMessage)
    assert result.content.startswith("[历史摘要]")
    assert "[T1]" in result.content


def test_summarize_messages_sync_entry() -> None:
    model = _FakeModel()
    result = asyncio.run(
        summarize_messages([HumanMessage(content="q")], query="意图", model=model)
    )
    assert result.content.startswith("[历史摘要]")
