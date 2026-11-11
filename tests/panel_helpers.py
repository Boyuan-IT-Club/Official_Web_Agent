"""合议测试的公共构造:按清单生成评委判定、批判者质疑。"""

from __future__ import annotations

from typing import Any

from official_agent.evaluation.panel.shapes import Critique, Judgement
from official_agent.evaluation.schema import (
    DEPARTMENTS,
    DEPT_MATCH_ITEMS,
    EFFORT_CHECKLIST,
    EFFORT_ITEMS,
    item_names,
)

RESUME_FIELDS = [
    {
        "field_key": "intro",
        "title": "个人简介",
        "value": "我是软件工程的新生,用 Flask 写过课程表查询小程序,已有两百名同学在用。",
    },
    {
        "field_key": "why",
        "title": "为什么加入博远",
        "value": "宣讲会上听学长讲社团官网是社员自己写的,想在技术部跟大家一起做项目。",
    },
]

TECH_BASE = ("技术部", "技术基础")
TECH_DRIVE = ("技术部", "探索内驱力")
TECH_SHARE = ("技术部", "分享意愿")
EFFORT_RICH = (EFFORT_CHECKLIST, "内容充实")
EFFORT_WRITTEN = (EFFORT_CHECKLIST, "表达成文")
EFFORT_SINCERE = (EFFORT_CHECKLIST, "加入意愿真挚")

QUOTE_FLASK = "用 Flask 写过课程表查询小程序"
QUOTE_WHY = "想在技术部跟大家一起做项目"


def item(name: str, quote: str = "") -> dict[str, Any]:
    """一个判定项:给了引文即达成,否则未达成。"""
    if quote:
        return {"item": name, "met": True, "quote": quote, "reason": "原文有具体事实"}
    return {"item": name, "met": False, "quote": "", "reason": "简历里没有写到"}


def judgement(
    met: dict[tuple[str, str], str] | None = None,
    *,
    attitude: str = "sincere",
    summary: str = "与技术部对得上,缺分享经历",
) -> Judgement:
    """评委判定。met:{(清单, 判定项): 引文},出现的条目判达成,其余判未达成。"""
    met = met or {}
    return Judgement(
        match=[
            {
                "dept": dept,
                "items": [
                    item(name, met.get((dept, name), ""))
                    for name in item_names(DEPT_MATCH_ITEMS[dept])
                ],
            }
            for dept in DEPARTMENTS
        ],
        effort=[
            item(name, met.get((EFFORT_CHECKLIST, name), "")) for name in item_names(EFFORT_ITEMS)
        ],
        summary=summary,
        attitude={"verdict": attitude, "reason": "各栏都写了具体事项"},
    )


def challenge(
    key: tuple[str, str], kind: str = "quote_not_supporting", *, quote: str = ""
) -> dict[str, str]:
    return {
        "checklist": key[0],
        "item": key[1],
        "kind": kind,
        "quote": quote,
        "reason": "引文撑不住这一项",
    }


def critique(
    *,
    spans: list[str] | None = None,
    challenges: list[dict[str, str]] | None = None,
    agrees_bad_faith: bool | None = None,
) -> Critique:
    return Critique(
        injection_spans=spans or [],
        challenges=challenges or [],
        agrees_bad_faith=agrees_bad_faith,
        dropped=0,
    )


def met_keys(settlement: Any) -> set[tuple[str, str]]:
    """汇总结果里判为达成的条目。"""
    keys = {(m["dept"], it["item"]) for m in settlement.match for it in m["items"] if it["met"]}
    return keys | {(EFFORT_CHECKLIST, it["item"]) for it in settlement.effort if it["met"]}


class ScriptedModel:
    """假模型:按顺序吐预设回复,并记下每次收到的 prompt。回复用完后重复最后一条。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def ainvoke(self, messages: list[Any], config: Any = None) -> Any:
        self.prompts.append(messages[0].content)
        reply = self.replies[min(len(self.prompts), len(self.replies)) - 1]

        class _Message:
            content = reply

        return _Message()


def scorecard_reply(judged: Judgement) -> str:
    """评委判定 → 评委模型该吐的 JSON 文本(评分 prompt 约定的输出格式)。"""
    import json

    return json.dumps(judged, ensure_ascii=False)


class PanelModel:
    """假模型:按 prompt 认出是评委、批判者还是答辩在调用,分别给预设回复。

    每个角色的回复可以是一个字符串(每次都回它)、一个列表(按次序回,用完重复
    最后一条)、一个函数(拿 prompt 算回复),或一个异常(调用即抛)。calls 记下
    各角色收到的 prompt。
    """

    def __init__(self, *, judge: Any, critic: Any = None, rebuttal: Any = None) -> None:
        self.scripts = {"judge": judge, "critic": critic, "rebuttal": rebuttal}
        self.calls: dict[str, list[str]] = {"judge": [], "critic": [], "rebuttal": []}
        self.usage: dict[str, int] | None = None

    @staticmethod
    def role_of(prompt: str) -> str:
        from official_agent.evaluation.panel import critic, judges
        from official_agent.prompt_loader import load_prompt

        if prompt.startswith(load_prompt(critic.PROMPT_FILE)):
            return "critic"
        return "rebuttal" if load_prompt(judges.PROMPT_FILE) in prompt else "judge"

    async def ainvoke(self, messages: list[Any], config: Any = None) -> Any:
        prompt = messages[0].content
        role = self.role_of(prompt)
        self.calls[role].append(prompt)
        script = self.scripts[role]
        if script is None:
            raise AssertionError(f"这个用例里不该调用{role}")
        if isinstance(script, Exception):
            raise script
        if callable(script):
            reply = script(prompt)
        else:
            replies = script if isinstance(script, list) else [script]
            reply = replies[min(len(self.calls[role]), len(replies)) - 1]
        usage = self.usage

        class _Message:
            content = reply
            usage_metadata = usage

        return _Message()

    @property
    def total_calls(self) -> int:
        return sum(len(prompts) for prompts in self.calls.values())
