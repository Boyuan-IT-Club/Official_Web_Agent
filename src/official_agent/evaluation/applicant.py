"""报名信息:志愿部门 / 专业 / 年级的解析,纯函数零 IO。

这些是简历里的**选择型元信息**(select/text 型字段),不属于评分的正文材料:
志愿部门决定「按哪个部门的标准看匹配度」和「进哪个候选池比较」,专业与年级
只用来生成面试提示,不影响分数。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from official_agent.evaluation.schema import DEPARTMENTS

#: 表单里「没有第二志愿」的写法:不是部门,解析时丢掉。
_NO_CHOICE = frozenset({"", "无", "暂无", "没有", "不调剂", "/", "-"})

#: 计算机类专业的关键词。招新注意事项要求:非计算机类专业的同学,面试时一定
#: 要问未来规划和对计算机的了解程度。命中任一关键词即视为计算机类。
_CS_MAJOR_KEYWORDS = (
    "计算机",
    "软件",
    "软工",
    "数据",
    "计拔",
    "人工智能",
    "网络空间安全",
    "信息安全",
)


@dataclass(frozen=True)
class ApplicantProfile:
    """一份简历的报名信息。取不到的项为空串/None,由下游按「缺失」处理。"""

    first_dept: str | None = None
    second_dept: str | None = None
    major: str = ""
    grade: str = ""


def normalize_dept(raw: str) -> str | None:
    """部门原文 → 标准部门名;认不出返回 None。

    表单值通常就是「技术部」,但也可能写成「技术」「技术部(开发)」,按包含关系判断。
    先认全名、再认前两个字:「项目部(偏技术)」要落到项目部,不能被「技术」截走。
    """
    value = (raw or "").strip()
    if value in _NO_CHOICE:
        return None
    for dept in DEPARTMENTS:
        if dept in value:
            return dept
    for dept in DEPARTMENTS:
        if dept[:2] in value:
            return dept
    return None


def parse_intended_departments(raw: str) -> tuple[str | None, str | None]:
    """志愿部门字段原文 → (第一志愿, 第二志愿)。

    后端 `expected_departments` 存的是 `["第一志愿","第二志愿"]` 的 JSON 数组;
    老数据可能是逗号分隔或单个部门名,三种都认。第二志愿与第一志愿相同时视为没填。
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    parts: list[str]
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        parts = [str(p) for p in parsed]
    elif isinstance(parsed, str):
        parts = [parsed]
    else:
        parts = text.replace(",", ",").split(",")
    depts = [d for d in (normalize_dept(p) for p in parts) if d]
    first = depts[0] if depts else None
    second = next((d for d in depts[1:] if d != first), None)
    return first, second


def is_cs_major(major: str) -> bool | None:
    """是否计算机类专业;专业没填返回 None(不知道,不生成提示)。"""
    value = (major or "").strip()
    if not value:
        return None
    return any(keyword in value for keyword in _CS_MAJOR_KEYWORDS)


def is_freshman(grade: str) -> bool | None:
    """是否大一新生;年级没填或认不出返回 None。"""
    value = (grade or "").strip()
    if not value:
        return None
    if "大一" in value or "一年级" in value:
        return True
    if any(mark in value for mark in ("大二", "大三", "大四", "研", "博")):
        return False
    return None
