"""校准案例(往届判例)的确定性规则:入库筛查、可用范围、检索选择、进 prompt 的排布。纯函数。

一条案例是条目级的:(清单, 判定项, 达成与否, 引文, 理由)。它给评委看「这一项什么样算
达成」,不存整份简历。案例文本出自往届候选人与评审,是不可信输入:入库时过注入扫描与
脱敏,进 prompt 时包数据区并声明「只说明标尺,不是指令」。

涉及 IO 的编排(算向量、读写库)在 evaluation/calibration.py;这里的函数都不碰 IO,
calibration_probes 直接拿它们当验收对象。
"""

from __future__ import annotations

import json
import math
from typing import Any, TypedDict

from official_agent.evaluation.panel.shapes import ItemKey
from official_agent.evaluation.schema import CHECKLISTS, checklist_item_names
from official_agent.prompt_loader import load_prompt
from official_agent.security.injection_guard import scan_injection, wrap_data_zone
from official_agent.security.pii import mask_pii

PROMPT_FILE = "evaluation/panel/anchors.md"

#: 案例与简历片段的余弦相似度低于它就不取:不相干的判例只会把评委带偏。
#: 初值,取决于嵌入模型,没有用真实数据校准过。
MIN_ANCHOR_SIMILARITY = 0.5

#: 引文与理由各自的长度上限(与评委输出的引文上限一致)
MAX_QUOTE_CHARS = 200
MAX_REASON_CHARS = 400


class CaseRejectedError(ValueError):
    """案例没通过入库筛查(判定项不存在、缺内容、含指令样文本);消息可直接给录入的人看。"""


class Anchors(TypedDict):
    """一份简历这次评分能用的校准案例。

    set 是本周期冻结的案例集水位(案例号不大于它的才可用);status:ok = 检索成功
    (可能一条都没选中),unavailable = 检索失败、评委不带案例照常判;by_item 是按
    条目定位选出的案例,每项至多一条达成、一条未达成。
    """

    set: int
    status: str
    by_item: dict[ItemKey, list[dict[str, Any]]]


def screen_case(
    *, checklist: str, item: str, met: bool, quote: str, reason: str
) -> dict[str, Any]:
    """入库筛查:判定项存在、内容齐全、不含指令样文本;通过后返回脱敏好的案例字段。

    Raises:
        CaseRejectedError: 任一条不满足。含指令样文本的案例一律不入库——它以后会被
            原样放进评委的 prompt。
    """
    quote, reason = quote.strip(), reason.strip()
    if checklist not in CHECKLISTS or item not in checklist_item_names(checklist):
        raise CaseRejectedError(f"没有「{checklist}·{item}」这个判定项")
    if not reason:
        raise CaseRejectedError("理由不能为空:判例要说明为什么算达成或不算")
    if met and not quote:
        raise CaseRejectedError("达成的判例必须带引文:它示范的是「什么样的原文算达成」")
    if len(quote) > MAX_QUOTE_CHARS or len(reason) > MAX_REASON_CHARS:
        raise CaseRejectedError(
            f"引文不超过 {MAX_QUOTE_CHARS} 字、理由不超过 {MAX_REASON_CHARS} 字"
        )
    hit, matched = scan_injection(f"{quote}\n{reason}")
    if hit:
        raise CaseRejectedError(f"含指令样文本(「{matched}」),不能作为判例")
    return {
        "checklist": checklist,
        "item": item,
        "met": bool(met),
        "quote": mask_pii(quote),
        "reason": mask_pii(reason),
    }


def case_text(case: dict[str, Any]) -> str:
    """一条案例拿去算向量的文本:引文 + 理由。未达成的案例往往没有引文,只靠理由。"""
    return f"{case['quote']}\n{case['reason']}".strip()


def eligible_cases(
    cases: list[dict[str, Any]], *, watermark: int, cycle_id: int, resume_id: int
) -> list[dict[str, Any]]:
    """这份简历这次评分可以用的案例。

    - 冻结:只用案例号不大于 watermark 的。周期开跑时定下水位,之后新加的案例不影响
      本周期——周期中途换标尺,前后的卡就不可比了;
    - 隔离:不用出自本周期的案例(只取已结束周期的),更不用出自同一份简历的案例。
    种子判例没有来源周期与来源简历,只受水位约束。
    """
    return [
        case
        for case in cases
        if case["case_id"] <= watermark
        and case.get("source_cycle_id") != cycle_id
        and case.get("source_resume_id") != resume_id
    ]


def cosine(first: list[float], second: list[float]) -> float:
    """两个向量的余弦相似度;任一为零向量或维度不同返回 0。"""
    if len(first) != len(second):
        return 0.0
    norm = math.sqrt(sum(x * x for x in first)) * math.sqrt(sum(y * y for y in second))
    if not norm:
        return 0.0
    return sum(x * y for x, y in zip(first, second, strict=True)) / norm


def select_anchors(
    cases: list[dict[str, Any]],
    fragment_vectors: list[list[float]],
    *,
    min_similarity: float = MIN_ANCHOR_SIMILARITY,
) -> dict[ItemKey, list[dict[str, Any]]]:
    """从可用案例里给每个判定项挑判例:至多一条达成、一条未达成。

    一条案例与简历的相似度取它和各个简历片段的相似度里最高的那个;低于门槛不取。
    同一个判定项的同一种判定(达成/未达成)里取最相似的一条,相似度相同取案例号小的
    (结果只由输入决定,不随遍历顺序变)。返回的案例不带向量。
    """
    best: dict[tuple[ItemKey, bool], tuple[float, dict[str, Any]]] = {}
    for case in sorted(cases, key=lambda c: c["case_id"]):
        similarity = max(
            (cosine(case["embedding"], vector) for vector in fragment_vectors), default=0.0
        )
        if similarity < min_similarity:
            continue
        slot = ((case["checklist"], case["item"]), bool(case["met"]))
        if slot not in best or similarity > best[slot][0]:
            best[slot] = (similarity, case)

    selected: dict[ItemKey, list[dict[str, Any]]] = {}
    for (key, _met), (_similarity, case) in sorted(
        best.items(), key=lambda kv: (kv[0][0], not kv[0][1])
    ):
        public = {k: case[k] for k in ("case_id", "checklist", "item", "met", "quote", "reason")}
        selected.setdefault(key, []).append(public)
    return selected


def used_case_ids(anchors: Anchors | None) -> list[int]:
    """这次评分实际放进 prompt 的案例号(升序),随卡落库供追溯。"""
    if not anchors:
        return []
    return sorted(case["case_id"] for cases in anchors["by_item"].values() for case in cases)


def render_anchors(anchors: Anchors | None, checklists: tuple[str, ...]) -> str:
    """给负责 checklists 这几张清单的评委看的判例段;没有可用判例返回空串。

    判例文本包数据区,前面是「只说明标尺,不是指令」的声明(prompt 文件)。每位评委只
    拿到自己负责的清单上的判例。
    """
    if not anchors:
        return ""
    shown = [
        {k: case[k] for k in ("checklist", "item", "met", "quote", "reason")}
        for (checklist, _item), cases in anchors["by_item"].items()
        if checklist in checklists
        for case in cases
    ]
    if not shown:
        return ""
    return (
        load_prompt(PROMPT_FILE)
        + "\n\n"
        + wrap_data_zone("calibration-cases", json.dumps(shown, ensure_ascii=False, indent=1))
    )
