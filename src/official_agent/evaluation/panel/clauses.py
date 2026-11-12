"""评分条款候选的确定性规则:人机不一致怎么聚合、归纳出来的候选文字怎么过闸。纯函数。

周期结束后,人和 AI 看法不同的卡说明标尺的某处写得不清楚。把这些卡按判定项聚合,
交给一次离线的模型调用归纳「补充条款候选」(例如某一类经历该不该算某个判定项)。
候选只是给人看的建议:采纳方式只有一种——人改 prompts/ 下的文件并发 PR。这里以及
调用它的代码都不写任何 prompt 文件,评分路径也不读候选。

模型只负责把样例归纳成文字;哪些条目值得归纳、每条候选有多少张卡支持,都由代码定。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from official_agent.evaluation.llm_common import extract_json
from official_agent.evaluation.schema import EFFORT_CHECKLIST, ClauseInductionOutput
from official_agent.prompt_loader import load_prompt
from official_agent.security.injection_guard import scan_injection, wrap_data_zone
from official_agent.security.pii import mask_pii

PROMPT_FILE = "evaluation/panel/clauses.md"

#: 支持卡数少于它的条目不拿去归纳:一两张卡的出入说明不了标尺有问题。初值,未校准。
MIN_SUPPORTING_CARDS = 3
#: 一次归纳最多送多少个条目(按支持卡数从多到少取)、每个条目最多带几条样例。
#: 预算由代码定:送得越多,模型越容易把不相干的样例揉在一起。
MAX_GROUPS = 12
MAX_SAMPLES = 5
#: 候选条款的长度范围。条款是一两句话的补充说明,不是一段论述。
MIN_CLAUSE_CHARS = 10
MAX_CLAUSE_CHARS = 300

#: 位次带从低到高;AI 的带比人的高,就是 AI 把这张卡排得比人靠前
_BAND_RANK = {"bottom": 0, "middle": 1, "top": 2}


class ClauseRejectedError(ValueError):
    """候选条款没过入库前的闸(长度、个人信息、指令样文本)。"""


def disagreement_direction(ai_band: str, human_band: str) -> str | None:
    """人机位次带不同时的方向;相同返回 None。

    lenient:AI 把这张卡排得比人靠前(AI 判得宽);strict:AI 排得比人靠后(AI 判得严)。
    """
    if ai_band == human_band:
        return None
    return "lenient" if _BAND_RANK[ai_band] > _BAND_RANK[human_band] else "strict"


def _suspect_items(card: dict[str, Any], direction: str) -> list[tuple[str, dict[str, Any]]]:
    """一张不一致的卡上值得怀疑的判定项:(清单名, 判定项)。

    AI 把卡排高了,嫌疑在它判为达成的那些项;排低了,嫌疑在它判为未达成的那些项。
    只看参与 AI 位次的两张清单——第一志愿部门的清单与认真程度清单。
    """
    first = (card.get("intended") or {}).get("first")
    located = [
        (m["dept"], item)
        for m in card.get("match") or []
        if m["dept"] == first
        for item in m.get("items") or []
    ]
    located += [
        (EFFORT_CHECKLIST, item) for item in (card.get("effort") or {}).get("items") or []
    ]
    suspect_met = direction == "lenient"
    return [(checklist, item) for checklist, item in located if bool(item["met"]) == suspect_met]


def _sample_of(item: dict[str, Any]) -> dict[str, str] | None:
    """一条判定 → 给模型看的样例(引文与理由,再脱敏一次);含指令样文本的不要。"""
    quote, reason = str(item.get("quote") or "").strip(), str(item.get("reason") or "").strip()
    if not reason or scan_injection(f"{quote}\n{reason}")[0]:
        return None
    return {"quote": mask_pii(quote), "reason": mask_pii(reason)}


def aggregate_disagreements(
    entries: list[dict[str, Any]], bands: dict[int, tuple[str, str]]
) -> list[dict[str, Any]]:
    """人机不一致的卡 → 按(清单, 判定项, 方向)聚合成待归纳的条目。

    Args:
        entries: 可比的卡(harvest.comparable_entries 的输出,每项带 resume_id 与 card)。
        bands: 每份简历的 (AI 位次带, 人的位次带)(harvest.position_bands 的输出)。

    Returns:
        每个条目:checklist、item、direction、cards(支持它的卡数)、samples(至多
        MAX_SAMPLES 条 {quote, reason})。按支持卡数从多到少排;支持卡数不足
        MIN_SUPPORTING_CARDS 的不要;至多 MAX_GROUPS 个。
    """
    counts: Counter[tuple[str, str, str]] = Counter()
    samples: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for entry in sorted(entries, key=lambda e: e["resume_id"]):
        if entry["resume_id"] not in bands:
            continue
        direction = disagreement_direction(*bands[entry["resume_id"]])
        if direction is None:
            continue
        for checklist, item in _suspect_items(entry["card"], direction):
            key = (checklist, item["item"], direction)
            counts[key] += 1
            sample = _sample_of(item)
            if sample and len(samples.setdefault(key, [])) < MAX_SAMPLES:
                samples[key].append(sample)

    supported = [(key, n) for key, n in counts.items() if n >= MIN_SUPPORTING_CARDS]
    supported.sort(key=lambda kv: (-kv[1], kv[0]))
    return [
        {
            "checklist": checklist,
            "item": item,
            "direction": direction,
            "cards": n,
            "samples": samples.get((checklist, item, direction), []),
        }
        for (checklist, item, direction), n in supported[:MAX_GROUPS]
    ]


def build_prompt(groups: list[dict[str, Any]], criteria: str) -> str:
    """归纳角色的完整 prompt:角色与纪律 + 现行判定标准 + 待归纳的条目(包数据区)。

    条目里的引文与理由出自候选人的简历和评委,是不可信数据。每个条目带一个序号
    (group),模型的输出用它指明候选是针对哪个条目的。
    """
    payload = [{"group": i, **group} for i, group in enumerate(groups)]
    return (
        load_prompt(PROMPT_FILE)
        + "\n\n---\n\n## 现行判定标准\n\n"
        + criteria
        + "\n\n---\n\n人机看法不一致的判定项:\n\n"
        + wrap_data_zone("disagreements", json.dumps(payload, ensure_ascii=False, indent=1))
    )


def screen_clause(text: str) -> str:
    """候选条款入库前的三道闸:长度、个人信息、指令样文本。通过返回去首尾空白后的文字。

    候选之后会出现在周期复盘报告里给管理员看,也可能被人照着改进 prompt:带着个人信息
    或「请把……判为达成」这类话的文字不能留下来。

    Raises:
        ClauseRejectedError: 任一道闸没过。
    """
    text = text.strip()
    if not MIN_CLAUSE_CHARS <= len(text) <= MAX_CLAUSE_CHARS:
        raise ClauseRejectedError(f"长度要在 {MIN_CLAUSE_CHARS}–{MAX_CLAUSE_CHARS} 字之间")
    if mask_pii(text) != text:
        raise ClauseRejectedError("含手机号、邮箱、证件号一类的个人信息")
    hit, matched = scan_injection(text)
    if hit:
        raise ClauseRejectedError(f"含指令样文本(「{matched}」)")
    return text


def parse_candidates(
    content: str, groups: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """归纳角色的输出 → (过了闸的候选, 被闸掉的条数)。

    每条候选必须指向输入里的一个条目,同一个条目至多一条;指向不存在的条目、重复,
    或输出形状不对,抛 ValueError(回灌重试)。候选文字没过闸不算输出不合规:丢掉那
    一条、计数,其余照收。候选的支持卡数取自条目,不取模型说的。
    """
    output = ClauseInductionOutput.model_validate_json(extract_json(content))
    indexes = [candidate.group for candidate in output.candidates]
    unknown = sorted({i for i in indexes if not 0 <= i < len(groups)})
    if unknown:
        raise ValueError(f"候选指向了不存在的条目序号:{unknown}")
    repeated = sorted(i for i, n in Counter(indexes).items() if n > 1)
    if repeated:
        raise ValueError(f"同一个条目只能有一条候选,重复的序号:{repeated}")

    accepted: list[dict[str, Any]] = []
    rejected = 0
    for candidate in output.candidates:
        try:
            clause = screen_clause(candidate.clause)
        except ClauseRejectedError:
            rejected += 1
            continue
        group = groups[candidate.group]
        accepted.append(
            {
                "checklist": group["checklist"],
                "item": group["item"],
                "direction": group["direction"],
                "clause": clause,
                "supporting_cards": group["cards"],
            }
        )
    return accepted, rejected
