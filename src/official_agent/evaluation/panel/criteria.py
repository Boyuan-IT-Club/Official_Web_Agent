"""判定标准的唯一出处:从评分 prompt 里按小节标题截取,不另写一份。纯函数。

评委、批判者、拆开之后的各位评委必须按同一套标准看「这一项什么样算达成」。标准写在
评分 prompt(prompts/evaluation/scoring.md)的清单表格里;这里按标题把需要的那几节
截出来。标题改了名就直接报错,而不是悄悄给模型一份空标准。
"""

from __future__ import annotations

from official_agent.evaluation.schema import DEPARTMENTS, EFFORT_CHECKLIST

_ALL_CHECKLISTS_START = "## 一、部门匹配清单"
_ALL_CHECKLISTS_END = "## 判定纪律"
_EFFORT_HEADING = "## 二、认真程度清单"
_ATTITUDE_HEADING = "## attitude"


def _section(text: str, heading: str) -> str:
    """以 heading 开头的那个 Markdown 小节:从标题行到下一个同级或更高级的标题之前。"""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(heading)), None)
    if start is None:
        raise ValueError(f"评分 prompt 里找不到「{heading}」小节")
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        hashes = len(lines[i]) - len(lines[i].lstrip("#"))
        if 0 < hashes <= level and lines[i][hashes : hashes + 1] == " ":
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def criteria_from(scoring_prompt: str) -> str:
    """两张清单的全部判定标准(四个部门 + 认真程度)。批判者复核整张卡时用。"""
    start = scoring_prompt.find(_ALL_CHECKLISTS_START)
    end = scoring_prompt.find(_ALL_CHECKLISTS_END)
    if start == -1 or end <= start:
        raise ValueError("评分 prompt 里找不到清单小节,无法提供判定标准")
    return scoring_prompt[start:end].strip()


def criteria_for(scoring_prompt: str, checklist: str) -> str:
    """一张清单的判定标准:某个部门的匹配清单,或认真程度清单。只判一张清单的评委用。

    Raises:
        ValueError: 清单名不存在,或评分 prompt 里没有对应的小节。
    """
    if checklist == EFFORT_CHECKLIST:
        return _section(scoring_prompt, _EFFORT_HEADING)
    if checklist not in DEPARTMENTS:
        raise ValueError(f"没有「{checklist}」这张清单")
    return _section(scoring_prompt, f"### {checklist}")


def attitude_rules_from(scoring_prompt: str) -> str:
    """态度三档(sincere / perfunctory / bad_faith)的定义与写法。"""
    return _section(scoring_prompt, _ATTITUDE_HEADING)
