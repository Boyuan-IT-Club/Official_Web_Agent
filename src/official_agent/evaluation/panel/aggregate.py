"""合议的汇总规则:评委判定 + 批判者质疑 + 答辩重判 → 最终判定与置信度。纯函数,零 IO。

模型只负责判定与质疑。分歧怎么记、哪些达成项作废、置信度落在哪一档,全部在这里
由代码派生。分数不在这里算(仍由达成项的权重派生);置信度是独立的字段,不进分数,
也不进等级——等级池里的卡要保持可比。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from official_agent.evaluation.grounding import spans_overlap
from official_agent.evaluation.panel.shapes import Critique, ItemKey, Judgement, iter_items
from official_agent.evaluation.schema import EFFORT_CHECKLIST

#: 第一志愿部门的清单上有这么多项分歧,整张卡就标低置信。初值,待用真实复核数据校准。
LOW_CONFIDENCE_FIRST_CHOICE_DISPUTES = 2

_INJECTION_OVERRIDE = "引文落在疑似提示注入的片段里,注入文本不能当证据,程序改判为未达成"
_BAD_FAITH_OVERRIDE = "态度不端经批判者确认,程序改判为未达成"
_NOTHING_MET = "AI 判定两张清单无一项达成,需重点复核"


@dataclass(frozen=True, slots=True)
class Settlement:
    """汇总结果:最终判定(与 Judgement 同形状的各部分)、是否重点复核、合议记录。

    panel 是落在卡上的合议记录:confidence / critic / injection / disputes /
    revised / overrides / attitude_dispute。整体理由不在这里:它由调用方按模式决定
    用谁写的(评委或主席),再经 annotate_summary 加说明。
    """

    match: list[dict[str, Any]]
    effort: list[dict[str, Any]]
    attitude: dict[str, str]
    hard_zero: bool
    hard_zero_reasons: dict[str, str]
    panel: dict[str, Any]


def _located(key: ItemKey, **extra: Any) -> dict[str, Any]:
    return {"checklist": key[0], "item": key[1], **extra}


def _confidence(
    *,
    critic_available: bool,
    injection_detected: bool,
    attitude_dispute: bool,
    disputes: list[dict[str, Any]],
    revised: list[ItemKey],
    first_dept: str | None,
) -> str:
    """置信度三档。分歧只产出置信度,不产出分数。

    low:批判者不可用(防线失效,交给人)、检出注入、态度有分歧,或第一志愿部门的
    清单上分歧太多;medium:有任何分歧,或评委在答辩中改判过;其余 high。
    """
    on_first_choice = sum(1 for d in disputes if first_dept and d["checklist"] == first_dept)
    if (
        not critic_available
        or injection_detected
        or attitude_dispute
        or on_first_choice >= LOW_CONFIDENCE_FIRST_CHOICE_DISPUTES
    ):
        return "low"
    return "medium" if disputes or revised else "high"


def annotate_summary(
    summary: str, panel: dict[str, Any], *, written_before_settlement: bool
) -> str:
    """给整体理由加上程序的说明,不改写理由本身。

    检出注入时在开头提醒:理由是模型写的,可能受那段文本影响。理由写在汇总之前
    (评委自己写的)而汇总又改动过判定时,在末尾注明以逐项判定为准;写在汇总之后的
    (主席写的)已经看过最终判定,不需要这一句。
    """
    if panel["injection"]["detected"]:
        summary = "【检出疑似提示注入:下面的整体理由可能受其影响,请对照原文与逐项判定】" + summary
    changed = len(panel["revised"]) + len(panel["overrides"])
    if written_before_settlement and changed:
        summary += f"(汇总时有 {changed} 项判定被改动,以逐项判定为准。)"
    return summary


def unreviewed_panel() -> dict[str, Any]:
    """绝对卡短路的卡的合议记录:没有模型判定,也就没有可质疑的东西。

    结论是确定性规则给的,所以置信度记 high;critic 记 skipped,与「批判者不可用」
    (unavailable)区分开。
    """
    return {
        "confidence": "high",
        "critic": "skipped",
        "injection": {"detected": False, "spans": []},
        "disputes": [],
        "revised": [],
        "overrides": [],
        "attitude_dispute": False,
        "judges": {},
    }


def settle(
    judgement: Judgement,
    *,
    critique: Critique | None,
    rebuttal: dict[ItemKey, dict[str, Any]] | None,
    first_dept: str | None,
) -> Settlement:
    """把一次合议收成最终判定。

    Args:
        critique: 批判者的质疑;None 表示批判者不可用(调用失败或输出两次都不合规)。
        rebuttal: 评委对被质疑条目的重判,按条目定位;None 表示没有答辩(没有质疑,
            或答辩失败)。有质疑却没有对应重判的条目记为分歧。
        first_dept: 第一志愿部门,只用于置信度(该部门清单上的分歧更要紧)。

    规则:
    1. 被质疑的条目:评委改判就用新判定;维持原判记为分歧(批判者没有改判权)。
    2. 达成项的引文与注入片段重叠 → 该项置为未达成。这是唯一由代码直接改判的情形。
    3. 态度不端要双钥:评委判 bad_faith 且批判者同意,才把全部判定置为未达成;
       批判者不同意则保留判定,记为态度分歧。
    """
    items = {key: dict(item) for key, item in iter_items(judgement)}
    challenges = critique["challenges"] if critique else []
    spans = critique["injection_spans"] if critique else []

    disputes: list[dict[str, Any]] = []
    revised: list[ItemKey] = []
    for challenge in challenges:
        key = (challenge["checklist"], challenge["item"])
        answer = (rebuttal or {}).get(key)
        if answer is not None and bool(answer["met"]) != bool(items[key]["met"]):
            revised.append(key)
            items[key] = dict(answer)
            continue
        if answer is not None:
            items[key] = dict(answer)
        disputes.append(
            _located(
                key,
                kind=challenge["kind"],
                judge_decision="upheld" if answer is not None else "no_response",
                reason=challenge["reason"],
            )
        )

    overrides: list[dict[str, Any]] = []

    def _force_unmet(key: ItemKey, why: str) -> None:
        reason = f"{why}。原判定理由:{items[key]['reason']}"
        items[key] = {**items[key], "met": False, "reason": reason}
        overrides.append(_located(key, reason=why))

    for key, item in list(items.items()):
        if item["met"] and any(spans_overlap(item["quote"], span) for span in spans):
            _force_unmet(key, _INJECTION_OVERRIDE)

    bad_faith = judgement["attitude"]["verdict"] == "bad_faith"
    confirmed_bad_faith = bool(bad_faith and critique and critique["agrees_bad_faith"] is True)
    attitude_dispute = bool(bad_faith and critique and not confirmed_bad_faith)
    if confirmed_bad_faith:
        for key, item in list(items.items()):
            if item["met"]:
                _force_unmet(key, _BAD_FAITH_OVERRIDE)

    any_met = any(item["met"] for item in items.values())
    panel = {
        "confidence": _confidence(
            critic_available=critique is not None,
            injection_detected=bool(spans),
            attitude_dispute=attitude_dispute,
            disputes=disputes,
            revised=revised,
            first_dept=first_dept,
        ),
        "critic": "ok" if critique is not None else "unavailable",
        "injection": {"detected": bool(spans), "spans": list(spans)},
        "disputes": disputes,
        "revised": [_located(key) for key in revised],
        "overrides": overrides,
        "attitude_dispute": attitude_dispute,
    }
    return Settlement(
        match=[
            {"dept": m["dept"], "items": [items[(m["dept"], it["item"])] for it in m["items"]]}
            for m in judgement["match"]
        ],
        effort=[items[(EFFORT_CHECKLIST, it["item"])] for it in judgement["effort"]],
        attitude=dict(judgement["attitude"]),
        hard_zero=confirmed_bad_faith or not any_met,
        hard_zero_reasons={} if any_met else {"_attitude": _NOTHING_MET},
        panel=panel,
    )
