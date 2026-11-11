"""合议各节点之间传递的数据形状。全是 plain dict:与评分卡同一种形状,可直接落卡。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, TypedDict

from official_agent.evaluation.schema import EFFORT_CHECKLIST

#: 一个判定项的定位:(清单名, 判定项名)
ItemKey = tuple[str, str]


class Judgement(TypedDict):
    """评委对一份简历的完整判定,部门与判定项都按清单顺序排好。

    判定项 dict 的键:item / met / quote / reason(同 schema.ItemVerdict)。
    """

    match: list[dict[str, Any]]  # [{"dept": 部门名, "items": [判定项, ...]}, ...]
    effort: list[dict[str, Any]]  # 认真程度清单的判定项
    summary: str
    attitude: dict[str, str]  # {"verdict": ..., "reason": ...}


class Critique(TypedDict):
    """批判者的输出经落地校验之后剩下的部分。

    injection_spans 都能在简历原文里找到;challenges 的条目都存在,且质疑类型与评委
    的判定对得上。没通过校验的条数记在 dropped 里。
    """

    injection_spans: list[str]
    challenges: list[dict[str, str]]  # 键同 schema.Challenge
    agrees_bad_faith: bool | None
    dropped: int


def iter_items(judgement: Judgement) -> Iterator[tuple[ItemKey, dict[str, Any]]]:
    """按清单顺序走完全部判定项:先四个部门,再认真程度。"""
    for dept_match in judgement["match"]:
        for item in dept_match["items"]:
            yield (dept_match["dept"], item["item"]), item
    for item in judgement["effort"]:
        yield (EFFORT_CHECKLIST, item["item"]), item
