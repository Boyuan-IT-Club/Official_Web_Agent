"""CV 深挖报告套件测试:样本扫描/脱敏口/优雅降级/报告结构。

不起真图(那要 LLM 与真实简历,是套件本身在本地跑的事);这里钉的是套件的
**契约**:缺样本不崩、报告过掩码、题目全文进报告、真实简历不入库。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from official_agent.evals import cv_dive_report as cvd

# ── 样本读取 ─────────────────────────────────────────


def test_resume_body_drops_human_notes(tmp_path: Path) -> None:
    """``=====`` 之后是人工批注,不是简历内容 —— 不能喂进图。

    批注是给人看的笔记;把它当材料等于用标注污染被测对象。
    """
    f = tmp_path / "1.txt"
    f.write_text("技术栈:\nPython\n=====\n我的看法:这里可以问得更细", encoding="utf-8")
    assert cvd._resume_body(f) == "技术栈:\nPython"


def test_resume_body_without_notes_keeps_all(tmp_path: Path) -> None:
    f = tmp_path / "2.txt"
    f.write_text("技术栈:\nGo", encoding="utf-8")
    assert cvd._resume_body(f) == "技术栈:\nGo"


# ── 环境自检:缺样本必须 SKIP,不得静默变绿 ─────────────


def test_env_blocker_reports_missing_samples(monkeypatch, tmp_path: Path) -> None:
    """没有样本 → 报明原因(引擎据此标 SKIP),不能谎报通过。"""
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(cvd, "_llm_blocker", lambda: None)
    blocker = cvd.env_blocker()
    assert blocker is not None
    assert "样本" in blocker


def test_env_blocker_passes_with_samples(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "1.txt").write_text("技术栈:Python", encoding="utf-8")
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path)
    monkeypatch.setattr(cvd, "_llm_blocker", lambda: None)
    assert cvd.env_blocker() is None


def test_env_blocker_defers_to_llm_check_first(monkeypatch, tmp_path: Path) -> None:
    """缺 LLM 配置时优先报 LLM(那是更根本的阻塞)。"""
    (tmp_path / "1.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path)
    monkeypatch.setattr(cvd, "_llm_blocker", lambda: "LLM_API_KEY 未配置")
    assert cvd.env_blocker() == "LLM_API_KEY 未配置"


# ── 脱敏口(红线)────────────────────────────────────


def test_mask_scrubs_phone_and_name() -> None:
    """报告出边界必须过既有 mask_pii_deep —— 题面会逐字引用简历原文。"""
    section = ["联系我 13812345678,姓名张三,邮箱 a@b.com"]
    masked = cvd._mask(section)
    joined = "".join(masked)
    assert "13812345678" not in joined
    assert "a@b.com" not in joined


# ── 题组结构计数 ─────────────────────────────────────


def _group() -> dict[str, Any]:
    return {
        "entry": {"question": "开场?"},
        "chains": [
            {
                "category": "C2_技术选型与权衡",
                "theme": "PyTorch",
                "layers": [
                    {"question": f"L{i}?", "answer_reference": {"strong": "s"}} for i in range(3)
                ],
            }
        ],
        "reserves": [{"question": "备选?"}],
    }


def test_survey_counts_structure() -> None:
    survey = cvd._survey({"group": _group()})
    assert survey == {
        "chains": 1,
        "chain_layers": 3,
        "reserves": 1,
        "entry": 1,
        "total_questions": 5,
        "layers_with_reference": 3,
    }


def test_survey_handles_empty_group() -> None:
    survey = cvd._survey({"group": {}})
    assert survey["total_questions"] == 0
    assert survey["chains"] == 0


# ── 报告渲染:题目全文必须出现 ────────────────────────


def test_render_group_includes_full_question_text() -> None:
    """人工审批要看全文,不能只给计数。"""
    lines = cvd._render_group(_group())
    text = "\n".join(lines)
    assert "开场?" in text
    assert "PyTorch" in text
    assert "L0?" in text and "L2?" in text
    assert "备选?" in text


def test_render_group_shows_three_tier_answers() -> None:
    group = {
        "chains": [
            {
                "theme": "Go",
                "layers": [
                    {
                        "question": "Go 是什么?",
                        "answer_reference": {"strong": "S", "acceptable": "A", "weak": "W"},
                    }
                ],
            }
        ]
    }
    text = "\n".join(cvd._render_group(group))
    assert "strong:S" in text and "acceptable:A" in text and "weak:W" in text


# ── 端到端(图与模型打桩)──────────────────────────


def _fake_cv_result(mode: str = "cv_dive") -> dict[str, Any]:
    return {
        "mode": mode,
        "prompt_version": "evaluation_cv_dive/v6",
        "group": {
            "entry": {
                "category": "C1_背景与动机",
                "question": "最想聊哪项技术?",
                "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
                "evidence": {"path": "", "note": "技术栈栏"},
                "time_minutes": 3,
            },
            "chains": [
                {
                    "category": "C2_技术选型与权衡",
                    "theme": "Python",
                    "layers": [
                        {
                            "question": "Python 是什么?",
                            "expected_signal": "说出解释器与常用库",
                            "answer_reference": {
                                "strong": "s",
                                "acceptable": "a",
                                "weak": "w",
                            },
                        }
                    ],
                }
            ],
            "reserves": [],
        },
    }


@pytest.mark.asyncio
async def test_run_suite_writes_masked_report(monkeypatch, tmp_path: Path) -> None:
    """端到端:报告落盘、含路由/技术栈数/题数/题目全文,且已过掩码。"""
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path)
    monkeypatch.setattr(cvd, "_REPORT_DIR", tmp_path / "reports")
    (tmp_path / "1.txt").write_text("技术栈:\nPython\n=====\n批注", encoding="utf-8")

    async def _fake_investigation(text, **kw):
        return _fake_cv_result()

    async def _fake_extract(text):
        from official_agent.evaluation.tech_stack import TechStackItem

        return [TechStackItem(name="Python", raw_text="技术栈:Python", claimed_level="listed")]

    monkeypatch.setattr(
        "official_agent.evaluation.investigate_graph.run_investigation", _fake_investigation
    )
    monkeypatch.setattr(
        "official_agent.evaluation.tech_stack.extract_tech_stack", _fake_extract
    )

    result = await cvd.run_suite(tmp_path / "cv_dive_report.yaml")
    report = (tmp_path / "reports" / "cv_dive_report.md").read_text(encoding="utf-8")

    assert result.status == "PASS"  # 报告模式:不设门禁
    assert result.metrics["samples"] == 1.0
    assert "cv_dive" in report  # 路由结果
    assert "技术栈" in report  # 抽出几个
    assert "Python 是什么?" in report  # 题目全文
    assert "批注" not in report  # 人工批注没被当材料用


@pytest.mark.asyncio
async def test_run_suite_survives_single_failure(monkeypatch, tmp_path: Path) -> None:
    """单份简历出题失败 → 照实记录,不炸整轮(其余样本仍出报告)。"""
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path)
    monkeypatch.setattr(cvd, "_REPORT_DIR", tmp_path / "reports")
    (tmp_path / "1.txt").write_text("技术栈:Python", encoding="utf-8")
    (tmp_path / "2.txt").write_text("技术栈:Go", encoding="utf-8")


    calls = {"n": 0}

    async def _flaky(text, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("上游 500")
        # 第二份给不同的技术主题,好断言「成功那份仍在报告里」
        result = _fake_cv_result()
        result["group"]["chains"][0]["theme"] = "Go"
        result["group"]["chains"][0]["layers"][0]["question"] = "Go 的 goroutine 是什么?"
        return result

    async def _fake_extract(text):
        return []

    monkeypatch.setattr(
        "official_agent.evaluation.investigate_graph.run_investigation", _flaky
    )
    monkeypatch.setattr("official_agent.evaluation.tech_stack.extract_tech_stack", _fake_extract)

    result = await cvd.run_suite(tmp_path / "cv_dive_report.yaml")
    assert result.metrics["samples"] == 2.0
    assert [c.passed for c in result.cases] == [False, True]
    report = (tmp_path / "reports" / "cv_dive_report.md").read_text(encoding="utf-8")
    assert "失败" in report  # 失败那份照实记

@pytest.mark.asyncio
async def test_report_masks_pii_quoted_in_questions(monkeypatch, tmp_path: Path) -> None:
    """题面逐字引用原文含手机号时,报告里必须是掩码后的形式。

    这是「落盘前过掩码」这条红线的实测:不是「碰巧模型没引用」,而是引用
    了也掩掉。
    """
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path)
    monkeypatch.setattr(cvd, "_REPORT_DIR", tmp_path / "reports")
    (tmp_path / "1.txt").write_text("技术栈:Python", encoding="utf-8")

    async def _leaky(text, **kw):
        return {
            "mode": "cv_dive",
            "group": {
                "entry": {
                    "category": "C1_背景与动机",
                    "question": "你的联系方式 13812345678 对吗?",
                    "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
                    "evidence": {"path": "", "note": "简历原文"},
                    "time_minutes": 3,
                },
                "chains": [],
                "reserves": [],
            },
        }

    async def _no_tech(text):
        return []

    monkeypatch.setattr("official_agent.evaluation.investigate_graph.run_investigation", _leaky)
    monkeypatch.setattr("official_agent.evaluation.tech_stack.extract_tech_stack", _no_tech)
    await cvd.run_suite(tmp_path / "cv_dive_report.yaml")
    report = (tmp_path / "reports" / "cv_dive_report.md").read_text(encoding="utf-8")
    assert "13812345678" not in report
    assert "138****5678" in report  # 掩码生效,不是整句被丢

@pytest.mark.asyncio
async def test_run_suite_no_samples_is_empty_not_crash(monkeypatch, tmp_path: Path) -> None:
    """样本目录不存在也不崩(引擎会在 env_blocker 处标 SKIP)。"""
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path / "missing")
    monkeypatch.setattr(cvd, "_REPORT_DIR", tmp_path / "reports")
    result = await cvd.run_suite(tmp_path / "cv_dive_report.yaml")
    assert result.metrics["samples"] == 0.0
    assert result.cases == []


@pytest.mark.asyncio
async def test_run_suite_json_summary_is_valid(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path)
    monkeypatch.setattr(cvd, "_REPORT_DIR", tmp_path / "reports")
    (tmp_path / "1.txt").write_text("技术栈:Python", encoding="utf-8")

    async def _fake(text, **kw):
        return _fake_cv_result()

    async def _no_tech(text):
        return []

    monkeypatch.setattr("official_agent.evaluation.investigate_graph.run_investigation", _fake)
    monkeypatch.setattr("official_agent.evaluation.tech_stack.extract_tech_stack", _no_tech)
    await cvd.run_suite(tmp_path / "cv_dive_report.yaml")
    data = json.loads((tmp_path / "reports" / "cv_dive_report.json").read_text(encoding="utf-8"))
    assert data["samples"] == 1
    assert data["cases"][0]["id"] == "1.txt"


@pytest.mark.asyncio
async def test_json_summary_is_masked_too(monkeypatch, tmp_path: Path) -> None:
    """机读摘要是第二条出边界,失败原因里内嵌的简历内容同样必须掩掉。

    校验异常会把链主题/题面片段写进 detail,只掩 markdown 会让 PII 从这份
    JSON 漏出去。
    """
    monkeypatch.setattr(cvd, "_RESUME_DIR", tmp_path)
    monkeypatch.setattr(cvd, "_REPORT_DIR", tmp_path / "reports")
    (tmp_path / "1.txt").write_text("技术栈:Python", encoding="utf-8")

    async def _leaky_failure(text, **kw):
        raise ValueError("链主题不在简历材料里(疑似编造):'13812345678'")

    async def _no_tech(text):
        return []

    monkeypatch.setattr(
        "official_agent.evaluation.investigate_graph.run_investigation", _leaky_failure
    )
    monkeypatch.setattr("official_agent.evaluation.tech_stack.extract_tech_stack", _no_tech)
    await cvd.run_suite(tmp_path / "cv_dive_report.yaml")

    raw = (tmp_path / "reports" / "cv_dive_report.json").read_text(encoding="utf-8")
    assert "13812345678" not in raw
    assert "138****5678" in raw  # 掩码生效,不是整条被丢
