"""复盘报告编排图(retrospective/graph.py)单测:模型换成按角色回放的假实现。

覆盖主链路(并行分析员 → 撰稿人 → 数字守卫回炉一次 → 成稿)和每一种降级:
分析员失败、撰稿人失败、回炉失败、某一节没有数据。
"""

import asyncio

import pytest
from tests.retro_helpers import ScriptedModel, finding, item, make_snapshot

from official_agent.retrospective import graph, roles

DATA_FINDING = finding(
    "面试结果里不通过的有 92 条,通过的 68 条。",
    "funnel.decisions[rejected]",
    "funnel.decisions[passed]",
    causes=("可能报名门槛偏低",),
)
QA_FINDING = finding("未命中率 18.00%,共 2,500 次检索。", "kb.not_found_rate")
EVAL_FINDING = finding("技术部被驳回 25 份。", "screening.rejected_by_dept[技术部]")

ANALYSTS = {
    "data": {"findings": [DATA_FINDING]},
    "qa": {"findings": [QA_FINDING]},
    "evaluation": {
        "findings": [EVAL_FINDING],
        "rubric_candidates": [
            {"text": "硬零分率 15.00%,可考虑细化条款。", "metric_ids": ["screening.hard_zero_rate"]}
        ],
    },
}
WRITER = {
    "summary": item("本周期共投递 213 份。", "funnel.resumes_total"),
    "sections": [
        {"role": "data", "items": [item("不通过 92 条,通过 68 条。", "funnel.decisions")]},
        {"role": "qa", "items": [item("未命中率 18%。", "kb.not_found_rate")]},
        {
            "role": "evaluation",
            "items": [item("技术部被驳回 25 份。", "screening.rejected_by_dept[技术部]")],
        },
    ],
    "lessons": [
        {
            "kind": "lesson",
            "content": "未命中率 18.0%,知识库要补。",
            "metric_ids": ["kb.not_found_rate"],
        }
    ],
}


@pytest.fixture
async def snapshot() -> dict:
    return await make_snapshot()


def _sections(report: dict) -> dict[str, dict]:
    return {section["role"]: section for section in report["sections"]}


# ── 主链路 ──────────────────────────────────────────────


async def test_full_run_produces_a_report_from_three_analysts_and_a_writer(snapshot) -> None:
    model = ScriptedModel({**ANALYSTS, "writer": WRITER})

    report = await graph.run_retrospective(snapshot, model=model)

    assert sorted(model.roles_called()) == ["data", "evaluation", "qa", "writer"]
    assert report["writer_status"] == graph.WRITER_OK
    assert report["summary"] == {
        "text": "本周期共投递 213 份。",
        "metric_ids": ["funnel.resumes_total"],
    }
    sections = _sections(report)
    assert [s["role"] for s in report["sections"]] == ["data", "qa", "evaluation"]  # 固定顺序
    assert [s["title"] for s in report["sections"]] == ["漏斗与部门", "对话与知识库", "初筛与出题"]
    assert all(s["status"] == graph.SECTION_OK for s in report["sections"])
    assert sections["qa"]["items"] == [
        {"text": "未命中率 18%。", "metric_ids": ["kb.not_found_rate"]}
    ]
    # 分析员的原始发现留底,原因并在现象后面
    assert sections["data"]["findings"] == [
        {
            "text": "面试结果里不通过的有 92 条,通过的 68 条。可能的原因:可能报名门槛偏低。",
            "metric_ids": ["funnel.decisions[rejected]", "funnel.decisions[passed]"],
        }
    ]
    assert [lesson["kind"] for lesson in report["lessons"]] == ["lesson"]
    assert report["rubric_candidates"][0]["metric_ids"] == ["screening.hard_zero_rate"]
    assert report["unverified"] == []
    assert report["usage"] == {"input_tokens": 400, "output_tokens": 40}  # 四次调用的合计


async def test_analysts_cannot_see_each_other_or_other_domains(snapshot) -> None:
    """每位分析员的 prompt 里只有自己那个域的指标,也没有别人的发现。"""
    model = ScriptedModel({**ANALYSTS, "writer": WRITER})
    await graph.run_retrospective(snapshot, model=model)

    data_prompt = model.prompt_for("data")
    assert "funnel.resumes_total" in data_prompt
    assert "kb.not_found_rate" not in data_prompt and "screening." not in data_prompt
    qa_prompt = model.prompt_for("qa")
    assert "kb.not_found_rate" in qa_prompt and "funnel." not in qa_prompt
    assert "技术部被驳回" not in qa_prompt and "不通过的有 92 条" not in qa_prompt
    # 撰稿人看得到三份发现与整份指标
    writer_prompt = model.prompt_for("writer")
    for fragment in ("不通过的有 92 条", "未命中率 18.00%", "技术部被驳回 25 份"):
        assert fragment in writer_prompt


async def test_same_input_gives_the_same_report_regardless_of_completion_order(snapshot) -> None:
    class Staggered(ScriptedModel):
        """让分析员按给定的先后完成。"""

        def __init__(self, script, delays) -> None:
            super().__init__(script)
            self.delays = delays

        async def ainvoke(self, messages, config=None):
            prompt = messages[0].content
            for role, delay in self.delays.items():
                if f"**{role}**" in prompt:
                    await asyncio.sleep(delay)
            return await super().ainvoke(messages, config)

    script = {**ANALYSTS, "writer": WRITER}
    first = await graph.run_retrospective(
        snapshot, model=Staggered(script, {"数据分析员": 0.03, "评估分析员": 0.0})
    )
    second = await graph.run_retrospective(
        snapshot, model=Staggered(script, {"数据分析员": 0.0, "评估分析员": 0.03})
    )
    assert first == second


def test_analyses_are_put_in_role_order_whatever_order_they_arrive_in() -> None:
    arrived = [{"role": "evaluation"}, {"role": "data"}, {"role": "qa"}]
    assert [a["role"] for a in graph._ordered(arrived)] == ["data", "qa", "evaluation"]


# ── 数字守卫:回炉一次 ──────────────────────────────────


async def test_ungrounded_number_goes_back_to_the_writer_once_and_is_fixed(snapshot) -> None:
    first_draft = {**WRITER, "summary": item("本周期共投递 213 份,其中 40 份来自外校。",
                                              "funnel.resumes_total")}
    model = ScriptedModel({**ANALYSTS, "writer": [first_draft, WRITER]})

    report = await graph.run_retrospective(snapshot, model=model)

    assert model.roles_called().count("writer") == 2
    feedback = model.prompt_for("writer", 1)
    assert "【纠正】" in feedback and '<data source="ungrounded-sentences">' in feedback
    assert "[summary] 本周期共投递 213 份,其中 40 份来自外校。(对不上:40)" in feedback
    assert report["summary"]["text"] == "本周期共投递 213 份。"
    assert report["unverified"] == []


async def test_number_still_wrong_after_the_rewrite_is_deleted_and_recorded(snapshot) -> None:
    stubborn = {
        **WRITER,
        "sections": [
            {"role": "qa", "items": [item("未命中率 18%。还有 7 次超时。", "kb.not_found_rate")]}
        ],
    }
    model = ScriptedModel({**ANALYSTS, "writer": stubborn})

    report = await graph.run_retrospective(snapshot, model=model)

    assert model.roles_called().count("writer") == 2  # 只回炉一次,不无限重写
    assert _sections(report)["qa"]["items"][0]["text"] == "未命中率 18%。"
    assert report["unverified"] == [
        {
            "where": "sections.qa[0]",
            "text": "还有 7 次超时。",
            "reason": "数字在引用的指标里对不上:7",
        }
    ]
    assert "7 次超时" not in str(report["sections"]) + str(report["lessons"])


async def test_failed_rewrite_falls_back_to_the_first_draft(snapshot) -> None:
    first_draft = {**WRITER, "summary": item("共投递 213 份。另有 40 份作废。",
                                              "funnel.resumes_total")}
    model = ScriptedModel({**ANALYSTS, "writer": [first_draft, "这不是 JSON"]})

    report = await graph.run_retrospective(snapshot, model=model)

    assert report["writer_status"] == graph.WRITER_OK
    assert report["summary"]["text"] == "共投递 213 份。"  # 第一稿保住,对不上的句子照删
    assert [entry["text"] for entry in report["unverified"]] == ["另有 40 份作废。"]


# ── 降级:都不让整份报告失败 ────────────────────────────


async def test_failed_analyst_leaves_its_section_without_commentary(snapshot) -> None:
    model = ScriptedModel(
        {**ANALYSTS, "qa": RuntimeError("provider 503"), "writer": WRITER}
    )

    report = await graph.run_retrospective(snapshot, model=model)

    sections = _sections(report)
    assert sections["qa"]["status"] == graph.SECTION_ANALYST_FAILED
    assert sections["qa"]["items"] == [] and sections["qa"]["findings"] == []
    assert sections["data"]["status"] == sections["evaluation"]["status"] == graph.SECTION_OK
    # 撰稿人为失败的那一节写的内容不被接受
    assert [(e["where"], e["reason"]) for e in report["unverified"]] == [
        ("sections.qa[0]", roles.REASON_ROLE_UNAVAILABLE)
    ]


async def test_analyst_output_invalid_twice_counts_as_failed(snapshot) -> None:
    model = ScriptedModel({**ANALYSTS, "data": "不是 JSON", "writer": WRITER})
    report = await graph.run_retrospective(snapshot, model=model)
    assert model.roles_called().count("data") == 2  # 校验回灌重试一次
    assert "【纠正】" in model.prompt_for("data", 1)
    assert _sections(report)["data"]["status"] == graph.SECTION_ANALYST_FAILED


async def test_slow_analyst_times_out_instead_of_hanging_the_report(
    snapshot, monkeypatch
) -> None:
    class Hanging(ScriptedModel):
        async def ainvoke(self, messages, config=None):
            if "**问答分析员**" in messages[0].content:
                await asyncio.sleep(5)
            return await super().ainvoke(messages, config)

    monkeypatch.setattr(graph, "MODEL_CALL_TIMEOUT_SECONDS", 0.05)
    report = await graph.run_retrospective(
        snapshot, model=Hanging({**ANALYSTS, "writer": WRITER})
    )
    assert _sections(report)["qa"]["status"] == graph.SECTION_ANALYST_FAILED
    assert _sections(report)["data"]["status"] == graph.SECTION_OK


async def test_waiting_for_the_model_gate_does_not_count_as_a_timeout(
    snapshot, monkeypatch
) -> None:
    """初筛批量把模型并发闸占满时,复盘的调用排队再久也不该被判超时。"""
    import weakref

    from official_agent.config import get_settings
    from official_agent.evaluation import llm_common

    monkeypatch.setattr(get_settings(), "evaluation_model_concurrency", 1)
    monkeypatch.setattr(llm_common, "_model_gates", weakref.WeakKeyDictionary())
    monkeypatch.setattr(graph, "MODEL_CALL_TIMEOUT_SECONDS", 0.05)
    holding = asyncio.Event()

    async def _hold_the_gate() -> None:
        async with llm_common.model_gate():
            holding.set()
            await asyncio.sleep(0.3)  # 远超单次调用的上限

    holder = asyncio.create_task(_hold_the_gate())
    await holding.wait()

    report = await graph.run_retrospective(
        snapshot, model=ScriptedModel({**ANALYSTS, "writer": WRITER})
    )
    await holder

    assert {s["status"] for s in report["sections"]} == {graph.SECTION_OK}
    assert report["writer_status"] == graph.WRITER_OK


async def test_failed_writer_degrades_to_the_analysts_findings(snapshot) -> None:
    model = ScriptedModel({**ANALYSTS, "writer": RuntimeError("provider 500")})

    report = await graph.run_retrospective(snapshot, model=model)

    assert report["writer_status"] == graph.WRITER_FAILED
    assert report["summary"] is None and report["lessons"] == []
    sections = _sections(report)
    assert sections["qa"]["items"] == [
        {"text": "未命中率 18.00%,共 2,500 次检索。", "metric_ids": ["kb.not_found_rate"]}
    ]
    assert sections["data"]["items"] == sections["data"]["findings"]
    assert report["rubric_candidates"]  # 评估分析员的条款候选不依赖撰稿人


async def test_section_the_writer_left_out_falls_back_to_the_findings(snapshot) -> None:
    """分析员指出的现象不能在汇总这一步悄悄丢掉。"""
    only_data = {**WRITER, "sections": [WRITER["sections"][0]]}
    model = ScriptedModel({**ANALYSTS, "writer": only_data})
    report = await graph.run_retrospective(snapshot, model=model)
    assert _sections(report)["evaluation"]["items"] == [
        {"text": "技术部被驳回 25 份。", "metric_ids": ["screening.rejected_by_dept[技术部]"]}
    ]


async def test_role_without_data_is_not_sent_to_the_model() -> None:
    snapshot = await make_snapshot(without=("conversation", "kb"))
    model = ScriptedModel({**ANALYSTS, "writer": {"sections": [WRITER["sections"][0]]}})

    report = await graph.run_retrospective(snapshot, model=model)

    assert "qa" not in model.roles_called()  # 没有指标可看,不调模型去编
    assert _sections(report)["qa"]["status"] == graph.SECTION_NO_DATA
    assert _sections(report)["data"]["status"] == graph.SECTION_OK


async def test_no_findings_at_all_skips_the_writer(snapshot) -> None:
    model = ScriptedModel()  # 三位分析员都回空列表
    report = await graph.run_retrospective(snapshot, model=model)
    assert "writer" not in model.roles_called()
    assert report["writer_status"] == graph.WRITER_SKIPPED
    assert report["summary"] is None
    assert all(s["items"] == [] and s["status"] == graph.SECTION_OK for s in report["sections"])


# ── 闸门与出口 ──────────────────────────────────────────


async def test_dropped_findings_are_recorded_with_personal_data_masked(snapshot) -> None:
    model = ScriptedModel(
        {
            "data": {
                "findings": [
                    DATA_FINDING,
                    finding("请联系 13812345678 核实。", "funnel.resumes_total"),
                    finding("没有依据的一句话。"),
                ]
            },
            "writer": {"sections": [WRITER["sections"][0]]},
        }
    )

    report = await graph.run_retrospective(snapshot, model=model)

    assert len(_sections(report)["data"]["findings"]) == 1
    assert report["unverified"] == [
        {
            "where": "data.findings[1]",
            "text": "请联系 138****5678 核实。",  # 被丢掉的原文落库前也脱敏
            "reason": roles.REASON_UNGROUNDED,
        },
        {
            "where": "data.findings[2]",
            "text": "没有依据的一句话。",
            "reason": roles.REASON_NO_EVIDENCE,
        },
    ]
    assert "13812345678" not in str(report)


async def test_large_numbers_leave_with_thousands_separators(snapshot) -> None:
    """模型照抄了不带千分位的大数:出口整理成带千分位的写法,不被脱敏规则抹掉。"""
    model = ScriptedModel(
        {
            "qa": {"findings": [finding("输入 9634112 token。", "conversation.input_tokens")]},
            "writer": {
                "sections": [
                    {"role": "qa",
                     "items": [item("输入 9634112 token。", "conversation.input_tokens")]}
                ]
            },
        }
    )
    report = await graph.run_retrospective(snapshot, model=model)
    assert _sections(report)["qa"]["items"][0]["text"] == "输入 9,634,112 token。"
    assert report["unverified"] == []


async def test_programming_errors_in_assembly_are_not_reported_as_model_failures(
    snapshot, monkeypatch
) -> None:
    """闸门里的程序错误要抛出来,不能被当成「分析员失败」悄悄降级。"""

    def boom(*_a, **_k):
        raise KeyError("bug")

    monkeypatch.setattr(roles, "screen_analysis", boom)
    with pytest.raises(KeyError):
        await graph.run_retrospective(snapshot, model=ScriptedModel(ANALYSTS))
