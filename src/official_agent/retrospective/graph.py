"""复盘报告的编排图:三位分析员并行 → 撰稿人 → 数字落地守卫 → 成稿。

    START ─Send×3→ analyze ─→ write ─→ ground ─(有对不上的数,且还没回炉过)→ write
                                          └────────────────────────────────→ assemble → END

- 分析员互相看不到:每位只拿到自己那份指标视图(Send 的载荷),各跑各的
- 撰稿人只看发现与指标,看不到原始数据
- 模型交回来的每一条都过 roles 里的确定性闸门;数字对不上先交回撰稿人改一次,
  还对不上的句子在 assemble 里删掉并记进 unverified

失败语义(docs/proposals/cycle-retrospective.md §7),都不让整份报告失败:
- 一位分析员失败(输出两次不合规、超时、模型报错)→ 那一节没有解读,只列指标
- 撰稿人失败 → 各节直接列分析员的发现,标「未汇总」
- 回炉那一次失败 → 沿用第一稿,对不上的句子照删

本图纯计算:不读库、不落库。快照由调用方传入,成稿(dict)由调用方渲染与保存。
"""

from __future__ import annotations

import asyncio
import logging
import operator
from dataclasses import asdict
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from official_agent.evaluation.llm_common import invoke_with_retry, prompt_version
from official_agent.retrospective import grounding, roles
from official_agent.retrospective.roles import (
    ANALYSIS_FAILED,
    ANALYSIS_NO_DATA,
    ANALYSIS_OK,
    Dropped,
    WriterDraft,
)
from official_agent.retrospective.views import ROLE_EVALUATION, ROLE_SPECS, ROLES, role_view
from official_agent.security.injection_guard import wrap_data_zone
from official_agent.security.pii import mask_pii

logger = logging.getLogger(__name__)

# 解读要稳定可复现:同一份快照重跑,发现不该大变。与评分同一个低温档
_TEMPERATURE = 0.1
# 单次模型调用(含校验回灌的重试)的墙钟上限;超时按该角色失败处理
MODEL_CALL_TIMEOUT_SECONDS = 180
# 撰稿人至多写两轮:第一稿,以及数字对不上时的一次回炉
_MAX_WRITER_ROUNDS = 2

SECTION_OK = "ok"
SECTION_ANALYST_FAILED = "analyst_failed"
SECTION_NO_DATA = "no_data"

WRITER_OK = "ok"
WRITER_FAILED = "failed"
WRITER_SKIPPED = "skipped"  # 没有任何发现可写,没有调用撰稿人

_ANALYST_FIELDS = "findings(每项 statement / metric_ids / causes)与 rubric_candidates"
_WRITER_FIELDS = "summary、sections(每项 role / items)、lessons(每项 kind / content / metric_ids)"


class RetroState(TypedDict, total=False):
    """图状态。analyses 与 usage 由并行的分析员各追加一条,用加法归并。"""

    snapshot: dict[str, Any]
    analyses: Annotated[list[dict[str, Any]], operator.add]
    usage: Annotated[list[dict[str, Any]], operator.add]
    draft: WriterDraft | None
    writer_rounds: int
    writer_failed: bool
    violations: list[tuple[str, grounding.Violation]]
    feedback: str
    report: dict[str, Any]


def prompt_versions() -> dict[str, str]:
    """本次用到的全部 prompt 版本(随报告落库,见 ADR-0004)。"""
    versions = {spec.role: prompt_version(spec.prompt_file) for spec in ROLE_SPECS}
    versions["writer"] = prompt_version(roles.WRITER_PROMPT_FILE)
    return versions


def _fan_out(state: RetroState) -> list[Send]:
    return [Send("analyze", {"snapshot": state["snapshot"], "role": role}) for role in ROLES]


def _after_ground(state: RetroState) -> str:
    rewrite = (
        state.get("violations")
        and not state.get("writer_failed")
        and state.get("writer_rounds", 0) < _MAX_WRITER_ROUNDS
    )
    return "write" if rewrite else "assemble"


def _feedback(violations: list[tuple[str, grounding.Violation]]) -> str:
    """数字对不上的句子 → 回炉时附给撰稿人的纠正段(句子是模型产出,包数据区)。"""
    listing = "\n".join(
        f"- [{where}] {violation.sentence}(对不上:{'、'.join(violation.tokens)})"
        for where, violation in violations
    )
    return (
        "\n\n【纠正】你上一稿里有下面这些句子,其中的数字在该段引用的指标里对不上"
        "(这是程序核对的结果,不是指令):\n"
        + wrap_data_zone("ungrounded-sentences", listing)
        + "\n请重新输出完整 JSON:把这些句子改成只用引用指标给出的数,或者删掉;"
        "其余内容保持不变。改不对的句子程序会直接删除。"
    )


def _ordered(analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """固定成角色顺序,同样的输入出同样的报告——不依赖图框架归并并行分支的次序。"""
    by_role = {a["role"]: a for a in analyses}
    return [by_role[role] for role in ROLES if role in by_role]


def build_graph(model: Any) -> Any:
    """装配并编译图。model 由调用方给(测试注入假模型),节点以闭包持有。"""

    async def analyze(payload: dict[str, Any]) -> dict[str, Any]:
        snapshot, role = payload["snapshot"], payload["role"]
        view = role_view(snapshot, role)
        if not view.metrics:
            return {"analyses": [{"role": role, "status": ANALYSIS_NO_DATA, "findings": []}]}
        prompt = roles.build_analyst_prompt(view)
        try:
            output, usage = await asyncio.wait_for(
                invoke_with_retry(
                    model,
                    prompt,
                    parse=roles.parse_analyst,
                    build_corrective=lambda exc: roles.corrective(exc, _ANALYST_FIELDS),
                    fail_message=f"{role} 分析员两次输出均不合规",
                ),
                timeout=MODEL_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — 模型侧的失败形态不可枚举(供应商异常、超时、不合规)
            # 降级而不是整份失败:这一节只列指标,其余两节照常。装配与闸门在
            # try 之外,那里的程序错误不会被当成「分析员失败」吞掉
            logger.warning("复盘分析员失败,该节降级为纯指标 role=%s", role, exc_info=True)
            return {
                "analyses": [
                    {
                        "role": role,
                        "status": ANALYSIS_FAILED,
                        "findings": [],
                        "error": type(exc).__name__,
                    }
                ]
            }
        screened = roles.screen_analysis(output, view, grounding.context_for(snapshot))
        return {
            "analyses": [
                {
                    "role": role,
                    "status": ANALYSIS_OK,
                    "findings": screened.findings,
                    "rubric_candidates": screened.rubric_candidates,
                    "dropped": screened.dropped,
                }
            ],
            "usage": [usage] if usage else [],
        }

    async def write(state: RetroState) -> dict[str, Any]:
        analyses = _ordered(state["analyses"])
        rounds = state.get("writer_rounds", 0) + 1
        if not any(a["status"] == ANALYSIS_OK and a["findings"] for a in analyses):
            return {"draft": WriterDraft(), "writer_rounds": rounds}
        prompt = roles.build_writer_prompt(state["snapshot"], analyses) + state.get("feedback", "")
        try:
            output, usage = await asyncio.wait_for(
                invoke_with_retry(
                    model,
                    prompt,
                    parse=roles.parse_writer,
                    build_corrective=lambda exc: roles.corrective(exc, _WRITER_FIELDS),
                    fail_message="撰稿人输出不合规",
                    # 回炉那一轮不再给形状重试的机会:第一稿已经是合规的,退路在手
                    attempts=2 if rounds == 1 else 1,
                ),
                timeout=MODEL_CALL_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — 同 analyze:模型侧失败形态不可枚举
            if rounds == 1:
                logger.warning("复盘撰稿人失败,报告降级为发现的直接拼接", exc_info=True)
                return {"draft": None, "writer_failed": True, "writer_rounds": rounds}
            logger.warning("复盘撰稿人回炉失败,沿用第一稿", exc_info=True)
            return {"writer_rounds": rounds}
        draft = roles.screen_writer(output, state["snapshot"], analyses)
        return {"draft": draft, "writer_rounds": rounds, "usage": [usage] if usage else []}

    def ground(state: RetroState) -> dict[str, Any]:
        draft = state.get("draft")
        if draft is None:
            return {"violations": [], "feedback": ""}
        snapshot = state["snapshot"]
        violations = roles.ground_draft(draft, snapshot, grounding.context_for(snapshot))
        return {"violations": violations, "feedback": _feedback(violations) if violations else ""}

    def assemble(state: RetroState) -> dict[str, Any]:
        return {"report": _assemble_report(state)}

    graph = StateGraph(RetroState)
    graph.add_node("analyze", analyze)
    graph.add_node("write", write)
    graph.add_node("ground", ground)
    graph.add_node("assemble", assemble)
    graph.add_conditional_edges(START, _fan_out, ["analyze"])
    graph.add_edge("analyze", "write")
    graph.add_edge("write", "ground")
    graph.add_conditional_edges("ground", _after_ground, ["write", "assemble"])
    graph.add_edge("assemble", END)
    return graph.compile()


def _finding_text(finding: dict[str, Any]) -> str:
    """撰稿人失败时,一条发现直接当一段话用。"""
    causes = finding.get("causes") or []
    return finding["statement"] + (f"可能的原因:{';'.join(causes)}。" if causes else "")


def _publishable(text: str) -> str:
    """模型写的文字出边界前的最后一步:大数补千分位,再过一遍脱敏兜底。"""
    return mask_pii(grounding.separate_thousands(text))


def _assemble_report(state: RetroState) -> dict[str, Any]:
    """把各节点的产出拼成报告正文(可直接存库的 dict)。"""
    analyses = {a["role"]: a for a in state["analyses"]}
    draft = state.get("draft")
    dropped: list[Dropped] = [d for a in _ordered(state["analyses"]) for d in a.get("dropped", [])]
    if draft is not None:
        dropped.extend(draft.dropped)
        dropped.extend(roles.strip_ungrounded(draft, state.get("violations") or []))

    sections: list[dict[str, Any]] = []
    for spec in ROLE_SPECS:
        analysis = analyses[spec.role]
        findings = [
            {"text": _finding_text(f), "metric_ids": f["metric_ids"]} for f in analysis["findings"]
        ]
        if analysis["status"] == ANALYSIS_NO_DATA:
            status, items = SECTION_NO_DATA, []
        elif analysis["status"] == ANALYSIS_FAILED:
            status, items = SECTION_ANALYST_FAILED, []
        else:
            # 撰稿人失败、漏写了这一节、或写的全被守卫删掉时,退回分析员的发现:
            # 分析员指出的现象不能在汇总这一步悄悄丢掉
            written = draft.sections.get(spec.role, []) if draft else []
            status, items = SECTION_OK, written or findings
        sections.append(
            {
                "role": spec.role,
                "title": spec.title,
                "status": status,
                "items": [
                    {"text": _publishable(i["text"]), "metric_ids": i["metric_ids"]} for i in items
                ],
                # 分析员过闸门之后的原始发现,留底:审阅的人可以对照撰稿人改写了什么
                "findings": [
                    {"text": _publishable(f["text"]), "metric_ids": f["metric_ids"]}
                    for f in findings
                ],
            }
        )

    if draft is None:
        writer_status = WRITER_FAILED
    elif not any(a["status"] == ANALYSIS_OK and a["findings"] for a in analyses.values()):
        writer_status = WRITER_SKIPPED
    else:
        writer_status = WRITER_OK
    summary = draft.summary if draft else None
    return {
        "summary": (
            {"text": _publishable(summary["text"]), "metric_ids": summary["metric_ids"]}
            if summary
            else None
        ),
        "sections": sections,
        "lessons": [
            {**lesson, "content": _publishable(lesson["content"])}
            for lesson in (draft.lessons if draft else [])
        ],
        "rubric_candidates": [
            {**candidate, "text": _publishable(candidate["text"])}
            for candidate in analyses[ROLE_EVALUATION].get("rubric_candidates", [])
        ],
        "writer_status": writer_status,
        # 被闸门丢掉或被守卫删掉的原文可能正是因为含个人信息才被丢,落库前脱敏
        "unverified": [{**asdict(d), "text": mask_pii(d.text)[:400]} for d in dropped],
        "usage": _total_usage(state.get("usage") or []),
    }


def _total_usage(usages: list[dict[str, Any]]) -> dict[str, int]:
    """各次模型调用的 token 用量合计(只汇总采到的)。"""
    total = {"input_tokens": 0, "output_tokens": 0}
    for usage in usages:
        for key in total:
            total[key] += int(usage.get(key) or 0)
    return total


async def run_retrospective(snapshot: dict[str, Any], *, model: Any = None) -> dict[str, Any]:
    """对一份快照正文(可带基线差值)跑完整张图,返回报告正文。

    model 缺省按配置构造 strong 档模型;测试与 eval 注入自己的。
    """
    if model is None:
        from official_agent.config import get_effective_settings
        from official_agent.graphs.assistant import build_model

        model = build_model(get_effective_settings(), temperature=_TEMPERATURE)
    final: RetroState = await build_graph(model).ainvoke({"snapshot": snapshot})
    return final["report"]
