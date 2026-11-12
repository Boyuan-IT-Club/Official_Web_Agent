"""拆开之后的评委的单测:一位评委只判一张清单。prompt 排布、各自的校验契约、失败语义。"""

import json

import pytest
from tests.panel_helpers import QUOTE_FLASK, QUOTE_WHY, RESUME_FIELDS, ScriptedModel, item

from official_agent.evaluation.applicant import ApplicantProfile
from official_agent.evaluation.material import resume_material, resume_sources
from official_agent.evaluation.panel import criteria, judges
from official_agent.evaluation.schema import (
    CHECKLISTS,
    DEPARTMENTS,
    DEPT_MATCH_ITEMS,
    EFFORT_CHECKLIST,
    EFFORT_ITEMS,
    item_names,
)
from official_agent.prompt_loader import load_prompt

SCORING_PROMPT = load_prompt("evaluation/scoring.md")
MATERIAL = resume_material(RESUME_FIELDS, ApplicantProfile(first_dept="技术部"))
SOURCES = resume_sources(RESUME_FIELDS)
TECH_ITEMS = item_names(DEPT_MATCH_ITEMS["技术部"])
EFFORT_NAMES = item_names(EFFORT_ITEMS)


def _dept_reply(met: dict[str, str] | None = None, names=TECH_ITEMS) -> str:
    met = met or {}
    return json.dumps({"items": [item(n, met.get(n, "")) for n in names]}, ensure_ascii=False)


def _effort_reply(
    met: dict[str, str] | None = None, verdict="sincere", reason="各栏都写了事"
) -> str:
    met = met or {}
    payload = {
        "items": [item(n, met.get(n, "")) for n in EFFORT_NAMES],
        "attitude": {"verdict": verdict, "reason": reason},
    }
    return json.dumps(payload, ensure_ascii=False)


def _parse(content: str, checklist: str = "技术部"):
    return judges.parse_checklist_verdicts(content, checklist=checklist, sources=SOURCES)


# ── 判定标准:每位评委只拿自己那张清单的 ──


@pytest.mark.parametrize("checklist", CHECKLISTS)
def test_criteria_for_one_checklist_cover_its_items_and_nobody_elses(checklist: str) -> None:
    own = criteria.criteria_for(SCORING_PROMPT, checklist)

    all_names = {c: set(item_names(DEPT_MATCH_ITEMS[c])) for c in DEPARTMENTS}
    all_names[EFFORT_CHECKLIST] = set(EFFORT_NAMES)
    for name in all_names[checklist]:
        assert f"| {name} |" in own
    for other, names in all_names.items():
        if other != checklist:
            assert not any(f"| {name} |" in own for name in names), other


def test_criteria_fail_loudly_for_unknown_checklist_or_missing_section() -> None:
    with pytest.raises(ValueError, match="没有「宣传部」这张清单"):
        criteria.criteria_for(SCORING_PROMPT, "宣传部")
    with pytest.raises(ValueError, match="找不到「### 技术部」小节"):
        criteria.criteria_for("## 一、部门匹配清单\n只有标题", "技术部")


def test_attitude_rules_define_all_three_verdicts() -> None:
    rules = criteria.attitude_rules_from(SCORING_PROMPT)

    for verdict in ("sincere", "perfunctory", "bad_faith"):
        assert verdict in rules
    assert "输出格式" not in rules  # 只截态度这一节,不带后面的小节


# ── prompt 排布:公共前缀逐字节相同,专属内容在后 ──


def test_every_judge_shares_one_byte_identical_prefix_ending_with_the_resume() -> None:
    """前言 + 简历材料对每位评委逐字节相同,模型侧的前缀缓存才命中;清单在它后面。"""
    prefix = judges.shared_prefix(MATERIAL)
    prompts = {c: judges.judge_prompt(c, MATERIAL, SCORING_PROMPT) for c in CHECKLISTS}

    assert prefix.startswith(load_prompt(judges.PREAMBLE_FILE))
    assert MATERIAL in prefix
    for prompt in prompts.values():
        assert prompt.startswith(prefix)
    tails = {c: p[len(prefix) :] for c, p in prompts.items()}
    assert len(set(tails.values())) == len(CHECKLISTS)  # 五位评委的尾部各不相同
    for checklist, tail in tails.items():
        assert "<data" not in tail  # 简历只出现在公共前缀里
        assert tail.endswith(" / ".join(item_names_of(checklist)))


def item_names_of(checklist: str) -> tuple[str, ...]:
    return (
        EFFORT_NAMES if checklist == EFFORT_CHECKLIST else item_names(DEPT_MATCH_ITEMS[checklist])
    )


def test_dept_judge_sees_only_its_own_department_and_no_attitude_rules() -> None:
    tail = judges.judge_prompt("技术部", MATERIAL, SCORING_PROMPT)[
        len(judges.shared_prefix(MATERIAL)) :
    ]

    assert "### 技术部" in tail
    for other in ("### 项目部", "### 媒体部", "### 综合部", "认真程度清单", "bad_faith"):
        assert other not in tail


def test_effort_judge_gets_the_effort_checklist_and_attitude_rules() -> None:
    prefix = judges.shared_prefix(MATERIAL)
    tail = judges.judge_prompt(EFFORT_CHECKLIST, MATERIAL, SCORING_PROMPT)[len(prefix) :]

    assert load_prompt(judges.EFFORT_PROMPT_FILE) in tail
    assert "| 内容充实 |" in tail and "bad_faith" in tail
    assert "### 技术部" not in tail


# ── 校验契约:分到各位评委自己的输出上 ──


def test_dept_judge_verdicts_come_back_in_checklist_order() -> None:
    shuffled = _dept_reply({"技术基础": QUOTE_FLASK}, names=tuple(reversed(TECH_ITEMS)))

    items, attitude = _parse(shuffled)

    assert [it["item"] for it in items] == list(TECH_ITEMS)
    assert [it["met"] for it in items] == [True, False, False, False]
    assert attitude is None  # 部门评委不判态度


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (_dept_reply(names=TECH_ITEMS[:3]), "技术部判定项不符:缺 .*社团参与意愿"),
        (_dept_reply(names=(*TECH_ITEMS, "技术基础")), "重复"),
        (_dept_reply(names=item_names(DEPT_MATCH_ITEMS["项目部"])), "多 .*组织统筹经历"),
        (
            json.dumps(
                {
                    "items": [
                        {"item": n, "met": True, "quote": "", "reason": "r"} for n in TECH_ITEMS
                    ]
                }
            ),
            "达成项缺原文引文",
        ),
        (_dept_reply({"技术基础": "我拿过图灵奖和三次 ACM 金牌"}), "引文非原文"),
        ('{"items": [], "attitude": {"verdict": "sincere", "reason": "r"}}', "items"),
        ("这份简历不错", "不含 JSON"),
    ],
    ids=[
        "漏项",
        "重复项",
        "判了别的部门的清单",
        "达成没给引文",
        "编造引文",
        "多出态度字段",
        "不是JSON",
    ],
)
def test_dept_judge_output_is_held_to_the_same_contract(reply: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse(reply)


def test_effort_judge_returns_attitude_with_its_verdicts() -> None:
    reply = _effort_reply({"内容充实": QUOTE_FLASK, "加入意愿真挚": QUOTE_WHY})

    items, attitude = _parse(reply, EFFORT_CHECKLIST)

    assert [it["item"] for it in items if it["met"]] == ["内容充实", "加入意愿真挚"]
    assert attitude == {"verdict": "sincere", "reason": "各栏都写了事"}


def test_effort_judge_must_keep_attitude_consistent_with_its_own_checklist() -> None:
    three_met = {"内容充实": QUOTE_FLASK, "加入意愿真挚": QUOTE_WHY, "表达成文": QUOTE_FLASK}

    with pytest.raises(ValueError, match="perfunctory 时认真程度至多达成 2 项"):
        _parse(_effort_reply(three_met, verdict="perfunctory"), EFFORT_CHECKLIST)
    with pytest.raises(ValueError, match="bad_faith 必须两张清单无一项达成"):
        _parse(_effort_reply({"内容充实": QUOTE_FLASK}, verdict="bad_faith"), EFFORT_CHECKLIST)
    with pytest.raises(ValueError, match="bad_faith reason 必须引述原文"):
        _parse(_effort_reply(verdict="bad_faith", reason="态度很差"), EFFORT_CHECKLIST)
    quoting = _effort_reply(verdict="bad_faith", reason=f"原文写的是「{SOURCES[0][:14]}」")
    assert _parse(quoting, EFFORT_CHECKLIST)[1]["verdict"] == "bad_faith"


def test_effort_judge_output_without_attitude_is_rejected() -> None:
    with pytest.raises(ValueError, match="attitude"):
        _parse(_dept_reply(names=EFFORT_NAMES), EFFORT_CHECKLIST)


# ── 调用 ──


async def test_judge_retries_once_and_reports_how_many_times() -> None:
    model = ScriptedModel(
        _dept_reply({"技术基础": "编造出来的一段原文引文"}), _dept_reply({"技术基础": QUOTE_FLASK})
    )

    result = await judges.judge_checklist(
        model, checklist="技术部", material=MATERIAL, scoring_prompt=SCORING_PROMPT, sources=SOURCES
    )

    assert result["checklist"] == "技术部"
    assert result["retries"] == 1
    assert result["attitude"] is None
    assert [it["met"] for it in result["items"]] == [True, False, False, False]
    assert '<data source="validator-error">' in model.prompts[1]


async def test_judge_that_never_complies_raises_with_its_checklist_name() -> None:
    model = ScriptedModel("我不想按格式输出")

    with pytest.raises(ValueError, match="认真程度评委两次输出均不合规"):
        await judges.judge_checklist(
            model,
            checklist=EFFORT_CHECKLIST,
            material=MATERIAL,
            scoring_prompt=SCORING_PROMPT,
            sources=SOURCES,
        )
    assert len(model.prompts) == 2
