"""报告渲染(retrospective/render.py)与后台生成任务(retrospective/reports.py)单测。

报告表被替换,不连 PG;真库上的版本与发布状态机在 test_retrospective_state_pg.py。
"""

import pytest
from tests.retro_helpers import ScriptedModel, finding, item, make_snapshot

from official_agent.retrospective import graph, reports
from official_agent.retrospective.render import render_markdown

SCRIPT = {
    "data": {"findings": [finding("共投递 213 份。", "funnel.resumes_total")]},
    "qa": {"findings": [finding("未命中率 18.00%。", "kb.not_found_rate")]},
    "evaluation": {
        "findings": [finding("技术部被驳回 25 份。", "screening.rejected_by_dept[技术部]")],
        "rubric_candidates": [
            {"text": "硬零分率 15.00%,可考虑细化条款。", "metric_ids": ["screening.hard_zero_rate"]}
        ],
    },
    "writer": {
        "summary": item("本周期共投递 213 份。", "funnel.resumes_total"),
        "sections": [
            {"role": "data", "items": [item("共投递 213 份。", "funnel.resumes_total")]},
            {"role": "qa", "items": [item("未命中率 18%。还有 7 次超时。", "kb.not_found_rate")]},
            {
                "role": "evaluation",
                "items": [item("技术部被驳回 25 份。", "screening.rejected_by_dept[技术部]")],
            },
        ],
        "lessons": [
            {"kind": "norm", "content": "未命中率 18.0%,每周补一次知识库。",
             "metric_ids": ["kb.not_found_rate"]}
        ],
    },
}


async def _report(snapshot: dict, script: dict | None = None) -> dict:
    return await graph.run_retrospective(snapshot, model=ScriptedModel(script or SCRIPT))


# ── 渲染 ────────────────────────────────────────────────


async def test_markdown_has_sections_citations_and_code_rendered_tables() -> None:
    snapshot = await make_snapshot()
    markdown = render_markdown(await _report(snapshot), snapshot, version=3)

    assert markdown.startswith("# 招新周期复盘报告 · 周期 12(第 3 版)\n")
    assert "没有对比基线" in markdown and "发布前需管理员审阅" in markdown
    for heading in ("## 概览", "## 漏斗与部门", "## 对话与知识库", "## 初筛与出题",
                    "## 候选教训", "## 评分条款候选", "## 被程序改动过的地方"):
        assert heading in markdown, heading
    # 依据写成人读的指标名,不是 id
    assert "1. 技术部被驳回 25 份。\n   依据:驳回(按部门) · 技术部" in markdown
    assert "1. 〔规矩〕未命中率 18.0%,每周补一次知识库。\n   依据:未命中率" in markdown
    # 指标表由程序从快照生成,带千分位;被抑制的格子写成范围
    assert "| 指标 | 本期 | 样本量 |" in markdown
    assert "| 输入 token | 9,634,112 token | 3,800 |" in markdown
    assert "| 驳回(按部门) · 项目部 | 不足 5 | — |" in markdown
    # 守卫删掉的句子不在正文里,只在末尾列出
    assert "未命中率 18%。\n" in markdown
    assert "- [sections.qa[0]] 还有 7 次超时。 —— 数字在引用的指标里对不上:7" in markdown
    assert markdown.count("7 次超时") == 1


async def test_markdown_shows_baseline_columns_when_compared() -> None:
    baseline = await make_snapshot(cycle_id=11, kb={
        "searches": 2000, "hits": 1600, "not_found": 400, "unavailable": 0, "low_score_hits": 300,
    })
    snapshot = await make_snapshot(baseline=baseline)
    markdown = render_markdown(await _report(snapshot), snapshot, version=1)
    assert "对比基线:周期 11" in markdown
    assert "| 指标 | 本期 | 样本量 | 基线 | 变化 |" in markdown
    assert "| 检索次数 | 2,500 | 2,500 | 2,000 | +500(+25.00%) |" in markdown
    assert "| 未命中率 | 18.00% | 2,500 | 20.00% | -2.00 个百分点 |" in markdown


async def test_markdown_marks_degraded_sections_instead_of_hiding_them() -> None:
    snapshot = await make_snapshot(without=("conversation", "kb"))
    script = {**SCRIPT, "evaluation": RuntimeError("provider down"),
              "writer": {"sections": [SCRIPT["writer"]["sections"][0]]}}
    markdown = render_markdown(await _report(snapshot, script), snapshot, version=1)
    assert "> 这一节的数据不可用。" in markdown and "合成快照没有提供这个域的数据" in markdown
    assert "> 这一节的分析员没有产出,下面只列指标,没有解读。" in markdown
    assert "| 初筛任务数 | 230 | 230 |" in markdown  # 没有解读,指标表照列


async def test_markdown_says_so_when_the_writer_failed() -> None:
    snapshot = await make_snapshot()
    report = await _report(snapshot, {**SCRIPT, "writer": RuntimeError("provider down")})
    markdown = render_markdown(report, snapshot, version=1)
    assert "> **未汇总**:撰稿人没有产出,以下各节直接列出分析员的发现。" in markdown
    assert "## 概览" not in markdown and "## 候选教训" not in markdown
    assert "1. 未命中率 18.00%。" in markdown


# ── 后台生成任务 ────────────────────────────────────────


class _Store:
    def __init__(self, monkeypatch, *, finish_result: bool = True) -> None:
        self.finished: list[dict] = []
        self.failed: list[tuple] = []
        self.finish_result = finish_result
        monkeypatch.setattr(reports.report_store, "finish_report", self._finish)
        monkeypatch.setattr(reports.report_store, "fail_report", self._fail)

    def _finish(self, cycle_id, version, **body) -> bool:
        self.finished.append({"cycle_id": cycle_id, "version": version, **body})
        return self.finish_result

    def _fail(self, cycle_id, version, error) -> bool:
        self.failed.append((cycle_id, version, error))
        return True


async def test_generate_saves_draft_with_versions_and_usage(monkeypatch) -> None:
    store = _Store(monkeypatch)
    snapshot = await make_snapshot()

    await reports.generate_report(
        cycle_id=12, version=3, snapshot=snapshot, model=ScriptedModel(SCRIPT)
    )

    (saved,) = store.finished
    assert (saved["cycle_id"], saved["version"]) == (12, 3)
    assert set(saved["sections"]) == {
        "summary", "sections", "lessons", "rubric_candidates", "writer_status",
    }
    assert saved["report_md"].startswith("# 招新周期复盘报告 · 周期 12(第 3 版)")
    assert [entry["text"] for entry in saved["unverified"]] == ["还有 7 次超时。"]
    assert saved["prompt_versions"]["writer"] == "retrospective_writer/v1"
    assert saved["prompt_versions"]["metrics_schema"] == snapshot["schema"]
    assert saved["prompt_versions"]["model"]  # 用的是哪个模型,随报告留底
    assert saved["usage"]["input_tokens"] > 0
    assert store.failed == []


async def test_generate_marks_the_version_failed_when_anything_breaks(monkeypatch) -> None:
    store = _Store(monkeypatch)

    async def _boom(*_a, **_k):
        raise RuntimeError("langgraph exploded at /srv/app/graph.py")

    monkeypatch.setattr(reports.graph, "run_retrospective", _boom)

    await reports.generate_report(cycle_id=12, version=3, snapshot={"schema": "x"})  # 不抛

    assert store.finished == []
    assert store.failed == [(12, 3, "生成失败(RuntimeError)")]  # 原因只留异常类名


async def test_generate_survives_a_failure_to_record_the_failure(monkeypatch, caplog) -> None:
    async def _boom(*_a, **_k):
        raise RuntimeError("x")

    def _cannot_write(*_a, **_k):
        raise OSError("pg down")

    monkeypatch.setattr(reports.graph, "run_retrospective", _boom)
    monkeypatch.setattr(reports.report_store, "fail_report", _cannot_write)
    with caplog.at_level("WARNING"):
        await reports.generate_report(cycle_id=12, version=3, snapshot={"schema": "x"})
    assert any("将在超时后被清理" in r.getMessage() for r in caplog.records)


async def test_late_result_for_a_version_no_longer_generating_is_dropped(
    monkeypatch, caplog
) -> None:
    """生成超过时限、版本已被判为失败:迟到的结果不保存,也不再把它标一次失败。"""
    store = _Store(monkeypatch, finish_result=False)
    snapshot = await make_snapshot()
    with caplog.at_level("WARNING"):
        await reports.generate_report(
            cycle_id=12, version=3, snapshot=snapshot, model=ScriptedModel(SCRIPT)
        )
    assert store.failed == []
    assert any("结果未保存" in r.getMessage() for r in caplog.records)


async def test_spawn_keeps_a_reference_until_the_task_finishes(monkeypatch) -> None:
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(**_kwargs) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(reports, "generate_report", _slow)
    reports.spawn_generation(cycle_id=12, version=1, snapshot={})
    await started.wait()
    assert len(reports._background_tasks) == 1  # 没有强引用的任务可能在完成前被回收
    release.set()
    await asyncio.sleep(0.01)
    assert reports._background_tasks == set()


@pytest.mark.parametrize("key", ["report_md", "sections"])
async def test_saved_report_contains_no_raw_identifiers(monkeypatch, key: str) -> None:
    """落库的正文里没有邮箱、手机号这类东西,即使模型把它们写进了发现。"""
    store = _Store(monkeypatch)
    script = {
        **SCRIPT,
        "data": {"findings": [
            finding("共投递 213 份。", "funnel.resumes_total"),
            finding("联系 a.b@example.com 或 13812345678。", "funnel.resumes_total"),
        ]},
    }
    await reports.generate_report(
        cycle_id=12, version=1, snapshot=await make_snapshot(), model=ScriptedModel(script)
    )
    text = str(store.finished[0][key]) + str(store.finished[0]["unverified"])
    assert "a.b@example.com" not in text and "13812345678" not in text
