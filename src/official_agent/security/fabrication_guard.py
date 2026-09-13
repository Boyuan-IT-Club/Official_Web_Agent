"""编造守卫(GRA-04 → #161):tools=[] 轮次的虚假查询声明拦截改写。

生产复现:unknown 用户 tools=[],模型仍回复「查询结果:…」。三层防线:
①装配层——工具清单与禁令进首条用户消息(compose_first_message);
②本输出守卫——回复出口确定性兜底,命中查询声明话术即整段改写;
③回归用例——等通用 runner(#148)落地后另票毕业(地图 Not yet specified)。

适用面:本守卫只挂 tools=[] 的轮次(unknown 档);有工具轮次的失败话术
约束在①里。守卫轻契约(#159/#163):guard_name=fabrication_empty_tools,
verdict=clean|triggered:xxx——调用方(cli/routes 改写点)以结构化日志落
guard_event,字段与 #163 injection_scan 同款。
"""

from __future__ import annotations

import re

#: 查询声明话术族(只在 tools=[] 轮次扫描,误伤面由调用方限定)
_CLAIM_RE = re.compile(
    r"查询结果|已经查询|已查询|我查了|帮你查|为您查询|查询到|检索到"
    r"|根据查询|系统里查|查到了|查到如下"
)
#: 否定前缀白名单:「没有查询结果」「无法查询到」是诚实话术,不触发
_NEGATION_RE = re.compile(r"(?:没有|无法|未能|不再|无)[^。]{0,6}$")

_HONEST_REPLY = (
    "我没有可用的数据查询权限,无法查询系统数据。"
    "如需查询简历、面试安排或统计信息,请登录对应系统或联系管理员处理。"
)

GUARD_NAME = "fabrication_empty_tools"


def guard_empty_tools_reply(text: str) -> tuple[str, str]:
    """tools=[] 轮次的回复出口守卫:返回 (最终文本, verdict)。

    verdict 形如 "clean" / "triggered:查询结果"。整段改写而非删句——
    编造声明与后文通常耦合,删句留半截话更糟。命中处前方(6 字窗)有否定
    词(没有/无法/未能…)视为诚实话术,不触发;「别」是安抚词不是否定
    (「别担心,查询到…」是典型安抚式编造),不进白名单。"""
    if not text:
        return text, "clean"
    for match in _CLAIM_RE.finditer(text):
        prefix = text[max(0, match.start() - 6) : match.start()]
        if _NEGATION_RE.search(prefix):
            continue
        return _HONEST_REPLY, f"triggered:{match.group(0)}"
    return text, "clean"
