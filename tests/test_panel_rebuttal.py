"""评委答辩的单测:prompt 追加在评委原 prompt 之后,重判与首次判定同一套契约。"""

import json

import pytest
from tests.panel_helpers import (
    EFFORT_RICH,
    EFFORT_SINCERE,
    EFFORT_WRITTEN,
    QUOTE_FLASK,
    QUOTE_WHY,
    RESUME_FIELDS,
    TECH_BASE,
    TECH_DRIVE,
    ScriptedModel,
    challenge,
    judgement,
)

from official_agent.evaluation.material import resume_sources
from official_agent.evaluation.panel import judges
from official_agent.evaluation.schema import CHECKLISTS, EFFORT_CHECKLIST
from official_agent.prompt_loader import load_prompt

SOURCES = resume_sources(RESUME_FIELDS)
BASE_PROMPT = "评委原来的完整 prompt(评分规则 + 简历材料 + 清单)"


def _reply(*verdicts: dict) -> str:
    return json.dumps({"verdicts": list(verdicts)}, ensure_ascii=False)


def _verdict(key, *, met: bool, quote: str = "", reason: str = "重看后的结论") -> dict:
    return {"checklist": key[0], "item": key[1], "met": met, "quote": quote, "reason": reason}


def _parse(content, judged, challenges, scope=CHECKLISTS):
    return judges.parse_rebuttal(
        content, judgement=judged, challenges=challenges, sources=SOURCES, attitude_scope=scope
    )


def test_prompt_appends_to_the_judges_prompt_and_wraps_challenges_as_data() -> None:
    """前缀与评委首次判定逐字节相同(模型侧前缀缓存才命中);质疑理由是数据不是指令。"""
    judged = judgement({TECH_BASE: QUOTE_FLASK})

    prompt = judges.build_rebuttal_prompt(BASE_PROMPT, judged, [challenge(TECH_BASE)])

    assert prompt.startswith(BASE_PROMPT)
    assert load_prompt(judges.REBUTTAL_PROMPT_FILE) in prompt
    # 答辩要求的正文里也提到了这个标签名;真正的数据区是最后一个
    zone = prompt[prompt.rindex('<data source="challenges">') :]
    payload = json.loads(zone[zone.index("\n") + 1 : zone.index("\n</data>")])
    assert payload == [
        {
            "checklist": "技术部",
            "item": "技术基础",
            "your_verdict": {"met": True, "quote": QUOTE_FLASK, "reason": "原文有具体事实"},
            "challenge": {
                "kind": "quote_not_supporting",
                "quote": "",
                "reason": "引文撑不住这一项",
            },
        }
    ]


def test_parse_returns_revised_items_keyed_by_location() -> None:
    judged = judgement({TECH_BASE: QUOTE_FLASK})
    reply = _reply(_verdict(TECH_BASE, met=False))

    revised = _parse(reply, judged, [challenge(TECH_BASE)])

    assert revised == {
        TECH_BASE: {"item": "技术基础", "met": False, "quote": "", "reason": "重看后的结论"}
    }


@pytest.mark.parametrize(
    ("verdicts", "message"),
    [
        ([], "verdicts"),  # 空清单过不了 schema
        ([_verdict(TECH_DRIVE, met=False)], "缺 .*技术基础"),
        (
            [_verdict(TECH_BASE, met=False), _verdict(TECH_DRIVE, met=False)],
            "多 .*探索内驱力",
        ),
        ([_verdict(TECH_BASE, met=False), _verdict(TECH_BASE, met=False)], "重复"),
    ],
)
def test_parse_requires_exactly_the_challenged_items(verdicts: list, message: str) -> None:
    judged = judgement({TECH_BASE: QUOTE_FLASK})

    with pytest.raises(ValueError, match=message):
        _parse(_reply(*verdicts), judged, [challenge(TECH_BASE)])


def test_parse_rejects_met_without_quote_and_quotes_not_in_resume() -> None:
    judged = judgement()
    missed = [challenge(TECH_DRIVE, "missed_evidence", quote=QUOTE_WHY)]

    with pytest.raises(ValueError, match="达成项缺原文引文"):
        _parse(_reply(_verdict(TECH_DRIVE, met=True)), judged, missed)
    with pytest.raises(ValueError, match="引文非原文"):
        _parse(_reply(_verdict(TECH_DRIVE, met=True, quote="我拿过图灵奖")), judged, missed)


def test_parse_keeps_attitude_and_verdicts_consistent_after_revision() -> None:
    """重判之后态度与达成仍要对得上:整体敷衍时认真程度至多达成 2 项。"""
    two_met = {EFFORT_RICH: QUOTE_FLASK, EFFORT_WRITTEN: QUOTE_FLASK}
    judged = judgement(two_met, attitude="perfunctory")
    third = [challenge(EFFORT_SINCERE, "missed_evidence", quote=QUOTE_WHY)]

    with pytest.raises(ValueError, match="perfunctory 时认真程度至多达成 2 项"):
        _parse(_reply(_verdict(EFFORT_SINCERE, met=True, quote=QUOTE_WHY)), judged, third)
    # 维持未达成不违例
    assert _parse(_reply(_verdict(EFFORT_SINCERE, met=False)), judged, third)


def test_parse_refuses_to_meet_anything_on_a_bad_faith_card() -> None:
    judged = judgement(attitude="bad_faith")
    missed = [challenge(TECH_BASE, "missed_evidence", quote=QUOTE_FLASK)]

    with pytest.raises(ValueError, match="bad_faith 必须两张清单无一项达成"):
        _parse(_reply(_verdict(TECH_BASE, met=True, quote=QUOTE_FLASK)), judged, missed)


def test_dept_judge_rebuttal_is_not_bound_by_an_attitude_it_never_saw() -> None:
    """拆开之后态度是认真程度评委判的,部门评委看不到:它在答辩里改判达成不算违例。

    态度不端时要不要清零整张卡,留给汇总的双钥去定。
    """
    judged = judgement(attitude="bad_faith")
    missed = [challenge(TECH_BASE, "missed_evidence", quote=QUOTE_FLASK)]
    reply = _reply(_verdict(TECH_BASE, met=True, quote=QUOTE_FLASK))

    assert _parse(reply, judged, missed, scope=())[TECH_BASE]["met"] is True
    # 认真程度评委自己的清单仍受它自己判的态度约束
    effort_missed = [challenge(EFFORT_SINCERE, "missed_evidence", quote=QUOTE_WHY)]
    with pytest.raises(ValueError, match="bad_faith 必须两张清单无一项达成"):
        _parse(
            _reply(_verdict(EFFORT_SINCERE, met=True, quote=QUOTE_WHY)),
            judged,
            effort_missed,
            scope=(EFFORT_CHECKLIST,),
        )


async def test_rebut_retries_once_then_returns_revised_items() -> None:
    judged = judgement({TECH_BASE: QUOTE_FLASK})
    model = ScriptedModel(
        _reply(_verdict(TECH_BASE, met=True, quote="编造的一段原文引文内容")),
        _reply(_verdict(TECH_BASE, met=True, quote="已有两百名同学在用")),
    )

    revised, _usage = await judges.rebut(
        model,
        base_prompt=BASE_PROMPT,
        judgement=judged,
        challenges=[challenge(TECH_BASE)],
        sources=SOURCES,
        attitude_scope=CHECKLISTS,
    )

    assert revised[TECH_BASE]["quote"] == "已有两百名同学在用"
    assert len(model.prompts) == 2
    assert model.prompts[0].startswith(BASE_PROMPT)
    assert '<data source="validator-error">' in model.prompts[1]


async def test_rebut_raises_when_the_judge_never_complies() -> None:
    model = ScriptedModel("我坚持原判,不需要重新输出")

    with pytest.raises(ValueError, match="评委答辩两次输出均不合规"):
        await judges.rebut(
            model,
            base_prompt=BASE_PROMPT,
            judgement=judgement({TECH_BASE: QUOTE_FLASK}),
            challenges=[challenge(TECH_BASE)],
            sources=SOURCES,
            attitude_scope=CHECKLISTS,
        )
