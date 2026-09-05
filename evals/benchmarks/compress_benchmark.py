"""压缩策略小型 benchmark(M6 #114 AC:候选策略跑小 benchmark 选定)。

对照两种候选策略在同一长会话上的「压缩后可答性」:
- S0 naive-truncate:直接丢弃 older,只保留近几轮(无摘要)
- S1 context-aware:ContextAware 摘要 + [T#] 引用(决策 #108 选定,#114 实现)

流程:构造含可回收事实的长会话 → 各策略压缩 → 强模型分别回答 3 个探针问题
(前 2 个答案在被压缩掉的 older 段,后 1 个在保留的近轮)→ 强模型按
0/1/2 打分(0 关键事实缺失 / 1 部分正确 / 2 完整正确),输出平均分与答案对照。

仅参考不落档(决策 #108);需要真实 LLM key,CI 不跑。
用法:uv run python evals/benchmarks/compress_benchmark.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from official_agent.config import get_settings  # noqa: E402
from official_agent.graphs.assistant.compression import (  # noqa: E402
    count_tokens,
    summarize_messages,
)

# ── 构造长会话:older 段埋 2 个可回收事实,近轮埋 1 个 ──────────────────────

_OLDER_FACTS = [
    ("我的内推码是什么来着?", "你的内推码是 BY2026,投递时填写即可。"),
    ("帮我记一下,我面试时间是哪天?", "你的面试安排在 9 月 12 日 14:00,线下进行。"),
]

_RECENT_FACTS = [
    ("面试地点怎么走?", "从地铁站 B 口出来步行 5 分钟,签到后上 3 楼。"),
]


def _build_conversation(turns: int = 14) -> list[BaseMessage]:
    """older 段:事实 + 填充闲聊交替;近轮:recent 事实。"""
    msgs: list[BaseMessage] = [
        HumanMessage(content="你好,我想咨询招新的事"),
        AIMessage(content="你好!关于招新有什么可以帮你?"),
    ]
    filler = [
        ("社团有哪些部门?", "有后端/前端/算法/运营四个组。"),
        ("平时活动多吗?", "每两周一次技术分享,不定期团建。"),
        ("需要自带电脑吗?", "面试不需要,入职后建议自备。"),
        ("可以跨组参与项目吗?", "可以,按兴趣自由组队。"),
    ]
    for i in range(turns):
        q, a = filler[i % len(filler)]
        msgs += [HumanMessage(content=q), AIMessage(content=a)]
        if i == turns // 2:
            for fq, fa in _OLDER_FACTS:
                msgs += [HumanMessage(content=fq), AIMessage(content=fa)]
    for fq, fa in _RECENT_FACTS:
        msgs += [HumanMessage(content=fq), AIMessage(content=fa)]
    return msgs


_PROBES = [
    _OLDER_FACTS[0][0].replace("是什么来着?", "是什么?"),
    _OLDER_FACTS[1][0].replace("帮我记一下,", ""),
    "签到之后要上几楼?",  # 改写措辞,避免与历史问题逐字相同
]

_JUDGE_PROMPT = (
    "你是评分器。根据【参考资料】判断【回答】是否包含【问题】所问的关键事实。\n"
    "评分:2=关键事实完整正确;1=部分正确或含糊;0=缺失/错误。\n"
    "只输出一个数字。\n\n问题:{q}\n参考资料:{ref}\n回答:{ans}\n评分:"
)

# 评分用的独立事实源(来自构造脚本,非被测系统输出)
_REFERENCE = {
    _PROBES[0]: "内推码是 BY2026",
    _PROBES[1]: "面试时间是 9 月 12 日 14:00",
    _PROBES[2]: "3 楼(从地铁站 B 口步行 5 分钟,签到后上 3 楼)",
}


async def _ask(model: Any, context: list[BaseMessage], question: str) -> str:
    """用压缩后的上下文回答探针问题(模拟下一轮对话)。"""
    msgs = [*context, HumanMessage(content=question)]
    resp = await model.ainvoke(msgs)
    return str(resp.content).strip()


async def _score(model: Any, question: str, answer: str) -> int:
    resp = await model.ainvoke(
        _JUDGE_PROMPT.format(q=question, ref=_REFERENCE[question], ans=answer)
    )
    text = str(resp.content).strip()
    for ch in text:
        if ch in "012":
            return int(ch)
    return 0


def _naive_truncate(messages: list[BaseMessage], keep: int = 6) -> list[BaseMessage]:
    return list(messages[-keep:])


async def _context_aware(messages: list[BaseMessage], model: Any) -> list[BaseMessage]:
    summary = await summarize_messages(messages[:-6], query="压缩后继续客服对话", model=model)
    return [summary, *messages[-6:]]


async def main() -> None:
    from official_agent.graphs.assistant import build_model

    settings = get_settings()
    model = build_model(settings, model=settings.model_strong).bind(temperature=0)

    convo = _build_conversation()
    print(f"会话规模:{len(convo)} 条消息 ≈ {count_tokens(convo)} tokens(cl100k)")

    strategies = {
        "S0 naive-truncate": _naive_truncate(convo),
        "S1 context-aware": await _context_aware(convo, model),
    }

    totals = {name: 0 for name in strategies}
    for name, ctx in strategies.items():
        print(f"\n=== {name}(压缩后 ≈ {count_tokens(ctx)} tokens)===")
        for probe in _PROBES:
            answer = await _ask(model, ctx, probe)
            score = await _score(model, probe, answer)
            totals[name] += score
            print(f"  [{score}] {probe}\n      → {answer}")

    print("\n=== 汇总(满分 6)===")
    for name, total in totals.items():
        print(f"  {name}: {total}/6")


if __name__ == "__main__":
    asyncio.run(main())
