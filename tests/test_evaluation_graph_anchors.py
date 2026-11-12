"""校准案例(往届判例)接进评分子图的测试:判例怎么进评委的 prompt、卡上记什么、哪些地方不该出现。

模型全是假的(tests/panel_helpers.PanelModel);判例由测试直接传给评分入口,不经过案例库。
"""

import json
from unittest.mock import patch

import pytest
from tests.panel_helpers import (
    EFFORT_SINCERE,
    QUOTE_FLASK,
    QUOTE_WHY,
    RESUME_FIELDS,
    TECH_BASE,
    PanelModel,
    challenge,
    judgement,
    scorecard_reply,
    split_judges,
)

from official_agent.evaluation import graph as ev
from official_agent.evaluation.applicant import ApplicantProfile
from official_agent.evaluation.panel import anchors
from official_agent.prompt_loader import load_prompt

TECH = ApplicantProfile(first_dept="技术部")
NO_FINDINGS = '{"injection_spans": [], "challenges": [], "agrees_bad_faith": null}'
CHAIR_REPLY = json.dumps({"summary": "与技术部对得上。"}, ensure_ascii=False)
JUDGED = judgement({TECH_BASE: QUOTE_FLASK, EFFORT_SINCERE: QUOTE_WHY})
NOTICE = load_prompt(anchors.PROMPT_FILE)
CASE_QUOTE = "往届同学用 Django 做过选课系统并上线"


def _case(case_id: int, checklist: str, item: str, *, met: bool = True) -> dict:
    return {
        "case_id": case_id,
        "checklist": checklist,
        "item": item,
        "met": met,
        "quote": CASE_QUOTE if met else "",
        "reason": "有做出来并上线的东西" if met else "只列了技术名词",
    }


def _anchors(*cases: dict, status: str = "ok") -> anchors.Anchors:
    by_item: dict = {}
    for case in cases:
        by_item.setdefault((case["checklist"], case["item"]), []).append(case)
    return anchors.Anchors(set=31, status=status, by_item=by_item)


FOUND = _anchors(
    _case(4, "技术部", "技术基础"),
    _case(9, "技术部", "技术基础", met=False),
    _case(17, "认真程度", "表达成文"),
)


class _Settings:
    model_strong = "test-strong"


async def _score(model: PanelModel, *, mode: str, found=FOUND, fields=None) -> dict:
    with (
        patch.object(ev, "build_model", lambda *a, **k: model),
        patch.object(ev, "get_effective_settings", lambda: _Settings()),
    ):
        return await ev.run_evaluation(
            fields or RESUME_FIELDS,
            resume_id=7,
            cycle_id=2026,
            profile=TECH,
            mode=mode,
            anchors=found,
        )


def _shown_cases(prompt: str) -> list[dict]:
    zone = prompt[prompt.rindex('<data source="calibration-cases">') :]
    return json.loads(zone[zone.index("\n") + 1 : zone.index("\n</data>")])


async def test_single_judge_sees_cases_for_every_checklist_as_data() -> None:
    model = PanelModel(judge=scorecard_reply(JUDGED), critic=NO_FINDINGS)

    card = await _score(model, mode="critic")

    (prompt,) = model.calls["judge"]
    assert NOTICE in prompt
    assert prompt.index("简历全文") < prompt.index(NOTICE)  # 判例排在简历与清单之后
    assert [c["item"] for c in _shown_cases(prompt)] == ["技术基础", "技术基础", "表达成文"]
    assert "case_id" not in prompt  # 案例号与来源不给模型
    assert card["panel"]["calibration"] == {"set": 31, "status": "ok", "used": [4, 9, 17]}
    assert card["versions"]["prompts"]["anchors"] == "evaluation_panel_anchors/v1"


async def test_critic_is_not_shown_the_cases() -> None:
    """判例是给评委把握尺度用的;批判者复核的是这份简历上的判定。"""
    model = PanelModel(judge=scorecard_reply(JUDGED), critic=NO_FINDINGS)

    await _score(model, mode="critic")

    assert CASE_QUOTE not in model.calls["critic"][0]
    assert NOTICE not in model.calls["critic"][0]


async def test_rebuttal_still_extends_the_judges_prompt_byte_for_byte() -> None:
    rebuttal = {
        "verdicts": [
            {
                "checklist": "技术部",
                "item": "技术基础",
                "met": True,
                "quote": QUOTE_FLASK,
                "reason": "维持",
            }
        ]
    }
    model = PanelModel(
        judge=scorecard_reply(JUDGED),
        critic=json.dumps(
            {"injection_spans": [], "challenges": [challenge(TECH_BASE)], "agrees_bad_faith": None},
            ensure_ascii=False,
        ),
        rebuttal=json.dumps(rebuttal, ensure_ascii=False),
    )

    await _score(model, mode="critic")

    assert model.calls["rebuttal"][0].startswith(model.calls["judge"][0])


async def test_each_split_judge_only_gets_cases_for_its_own_checklist() -> None:
    model = PanelModel(**split_judges(JUDGED), critic=NO_FINDINGS, chair=CHAIR_REPLY)

    card = await _score(model, mode="panel")

    assert [c["checklist"] for c in _shown_cases(model.calls["技术部"][0])] == ["技术部", "技术部"]
    assert [c["checklist"] for c in _shown_cases(model.calls["effort"][0])] == ["认真程度"]
    for role in ("项目部", "媒体部", "综合部", "chair"):
        assert NOTICE not in model.calls[role][0]  # 没有自己清单上的判例就不带这一段
    assert card["panel"]["calibration"]["used"] == [4, 9, 17]


async def test_text_from_a_case_cannot_be_quoted_as_this_resumes_evidence() -> None:
    """判例不是这份简历的内容:评委把判例里的原文抄成引文,引文校验照样打回。"""
    copied = judgement({TECH_BASE: CASE_QUOTE})
    model = PanelModel(judge=scorecard_reply(copied), critic=NO_FINDINGS)

    with pytest.raises(RuntimeError, match="引文非原文"):
        await _score(model, mode="critic")
    assert len(model.calls["judge"]) == 2  # 回灌重试一次后放弃


async def test_single_mode_never_uses_cases_even_if_given() -> None:
    model = PanelModel(judge=scorecard_reply(JUDGED))

    card = await _score(model, mode="single")

    assert NOTICE not in model.calls["judge"][0]
    assert "panel" not in card


async def test_unavailable_retrieval_is_recorded_and_judges_go_on_without_cases() -> None:
    model = PanelModel(judge=scorecard_reply(JUDGED), critic=NO_FINDINGS)

    card = await _score(model, mode="critic", found=_anchors(status="unavailable"))

    assert NOTICE not in model.calls["judge"][0]
    assert card["panel"]["calibration"] == {"set": 31, "status": "unavailable", "used": []}
    assert "anchors" not in card["versions"]["prompts"]
    assert card["panel"]["confidence"] == "high"  # 判例是增强不是防线,取不到不影响置信度


async def test_cycle_without_calibration_leaves_no_trace_on_the_card() -> None:
    model = PanelModel(judge=scorecard_reply(JUDGED), critic=NO_FINDINGS)

    card = await _score(model, mode="critic", found=None)

    assert "calibration" not in card["panel"]
    assert NOTICE not in model.calls["judge"][0]


async def test_hard_zero_card_still_records_the_frozen_set() -> None:
    """周期冻结的水位取自第一张卡:空白卷的卡也得把水位记上。"""
    blank = [{"field_key": "intro", "title": "个人简介", "value": "111"}]

    card = await _score(PanelModel(), mode="critic", fields=blank)

    assert card["hard_zero"] is True
    assert card["panel"]["calibration"] == {"set": 31, "status": "skipped", "used": []}
