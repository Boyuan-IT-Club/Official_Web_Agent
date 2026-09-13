"""评分子图(B1,#123/#126):precheck(绝对卡) → 条件分支 → llm_score → finalize。

- 一总图两子图中的「评分子图」;调查子图(B3)与它平行
- 确定性规则优先:任一打分维命中绝对卡 → 整份硬 0,不调模型(省钱+可测)
- LLM 轨:model_strong + 低温 0.1 + 提示词 JSON + strict Pydantic 校验
  (检查点③实测:思考模式代理拒 json_schema 与强制 tool_choice)
- 输出是**卡 dict**(schema evaluation_scorecard/v1),落库由调用方
  (state/evaluation.py,B2 接线)负责;本图纯计算无 DB IO
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from official_agent.config import get_effective_settings
from official_agent.evaluation.schema import ScorecardOutput
from official_agent.evaluation.scoring import (
    FieldText,
    detect_hard_zero,
    weighted_total,
)
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt, load_prompt_meta
from official_agent.security.injection_guard import wrap_data_zone

PROMPT_FILE = "evaluation/scoring.md"
CARD_SCHEMA_VERSION = "evaluation_scorecard/v1"
SCORING_TEMPERATURE = 0.1


def _prompt_version() -> str:
    """prompt frontmatter 的 version(ADR-0004:文件是唯一权威)。"""
    return load_prompt_meta(PROMPT_FILE).get("version", "unknown")


class EvaluationState(TypedDict, total=False):
    """子图状态。fields 元素:{field_key,title,value}(plain dict,可序列化)。"""

    resume_id: int
    cycle_id: int
    fields: list[dict[str, str]]
    weights: dict[str, float]
    hard_zero: bool
    hard_zero_reasons: dict[str, str]
    card: dict[str, Any]
    error: str | None
    llm_usage: dict[str, int | None]  # #183:评分模型调用的 token 用量(job 观测面)


def _as_field_texts(fields: list[dict[str, str]]) -> list[FieldText]:
    return [
        FieldText(
            field_key=str(f["field_key"]),
            title=str(f.get("title", "")),
            value=str(f.get("value", "")),
            placeholder=str(f.get("placeholder", "")),
        )
        for f in fields
    ]


def _extract_json(text: str) -> str:
    """从模型回复中截取首个完整 JSON 对象(容忍代码围栏/前后杂文)。

    raw_decode 而非 rfind:尾随杂文含 `}` 时 rfind 会切进噪声产出非法
    JSON(B2 评审 P2);raw_decode 取首个完整对象,天然正确。
    """
    import json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"模型回复不含 JSON:{cleaned[:120]}")
    try:
        _, end = json.JSONDecoder().raw_decode(cleaned, start)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败:{cleaned[:120]}") from exc
    return cleaned[start:end]


async def precheck(state: EvaluationState) -> dict:
    """绝对卡确定性短路:任一维命中 → 整份硬 0(#123)。"""
    reasons = detect_hard_zero(_as_field_texts(state["fields"]))
    return {"hard_zero": bool(reasons), "hard_zero_reasons": reasons}


def route_after_precheck(state: EvaluationState) -> str:
    return "finalize_hard" if state.get("hard_zero") else "llm_score"


async def finalize_hard(state: EvaluationState) -> dict:
    """硬 0 卡:全部打分维 0 分,依据=命中原因,不调模型。"""
    reasons = state.get("hard_zero_reasons", {})
    settings = get_effective_settings()
    dimensions = [
        {
            "field_key": f["field_key"],
            "score": 0,
            "rationale": f"态度不端硬 0:{reasons.get(f['field_key'], '命中绝对卡规则')}",
            "evidence": (f.get("value") or "")[:80],
        }
        for f in state["fields"]
    ]
    card = {
        "schema": CARD_SCHEMA_VERSION,
        "resume_id": state["resume_id"],
        "cycle_id": state["cycle_id"],
        "dimensions": dimensions,
        "attitude": {
            "verdict": "bad_faith",
            "reason": "确定性绝对卡短路:" + ";".join(sorted(reasons.values())),
        },
        "total": 0.0,
        "hard_zero": True,
        "hard_zero_reasons": reasons,
        "versions": {
            "prompt": _prompt_version(),
            "weights": "cycle-config",
            "model": settings.model_strong,
        },
    }
    return {"card": card, "error": None, "llm_usage": {}}


def _evidence_in(evidence: str, source: str) -> bool:
    """证据逐字性:归一空白后 evidence 必须是原文子串(B1 评审 P1-2)。"""

    def norm(s: str) -> str:
        return "".join(s.split())

    ev = norm(evidence)
    return bool(ev) and ev in norm(source)


async def llm_score(state: EvaluationState, config: RunnableConfig | None = None) -> dict:
    """结构化打分:逐维给分+依据+原文证据,态度判定;异常进 error(B2 可重试)。"""
    try:
        settings = get_effective_settings()
        model = build_model(settings, temperature=SCORING_TEMPERATURE)
        # 结构化输出轨(检查点③实测拍板):当前代理的模型全是思考模式,
        # json_schema response_format 与强制 tool_choice 均被拒(400)——
        # 落到提示词 JSON 轨:模型输出 JSON 文本,strict Pydantic 校验
        # (schema.py extra=forbid)兜住形状;解析失败进 error 态由 B2 重试
        blocks = [
            "### "
            + (f.get("title") or f["field_key"])
            + f" (field_key={f['field_key']})\n"
            # #163:简历=不可信输入,原文包数据区标签(prompt 侧配数据区纪律)
            + wrap_data_zone(f"resume:{f['field_key']}", str(f.get("value", "")))
            for f in state["fields"]
        ]
        prompt_text = (
            load_prompt(PROMPT_FILE)
            + "\n\n---\n\n简历各维原文:\n\n"
            + "\n\n".join(blocks)
            + "\n\nfield_key 取值必须是:"
            + ",".join(f["field_key"] for f in state["fields"])
        )
        resp = await model.ainvoke(
            [HumanMessage(content=prompt_text)],
            config=config,
        )
        llm_usage = None
        um = getattr(resp, "usage_metadata", None)
        if um:
            from official_agent.state.conversation import extract_usage

            llm_usage = extract_usage(um)
        raw = resp.content
        if isinstance(raw, list):  # 思考模型可能回块列表:只拼 text 块
            raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
        content = raw if isinstance(raw, str) else str(raw)
        result = ScorecardOutput.model_validate_json(_extract_json(content))
        # strict 后置校验(评审 P1):模型漏维/造维、证据非原文都属静默降级,
        # 在这里翻进 error 态走 B2 重试,绝不落成"看起来完整"的卡
        expected = [f["field_key"] for f in state["fields"]]
        got = [d.field_key for d in result.dimensions]
        if sorted(got) != sorted(expected):
            raise ValueError(
                f"维度集不完整:缺 {sorted(set(expected) - set(got))},"
                f"多 {sorted(set(got) - set(expected))}"
            )
        sources = {f["field_key"]: f.get("value", "") for f in state["fields"]}
        for d in result.dimensions:
            if not _evidence_in(d.evidence, sources.get(d.field_key, "")):
                raise ValueError(f"证据非原文(field_key={d.field_key}):{d.evidence[:40]!r}")
        # #162 硬校验(#157 决议 §4):态度与分数的契约,违例翻 error 重试
        if result.attitude.verdict == "bad_faith" and any(d.score != 0 for d in result.dimensions):
            raise ValueError("bad_faith 必须全维 0(模型给了非 0 分)")
        if result.attitude.verdict == "perfunctory" and any(
            d.score > 30 for d in result.dimensions
        ):
            raise ValueError("perfunctory 必须全维 ≤30(模型给了高分)")
        if result.attitude.verdict == "bad_faith" and not any(
            fk in result.attitude.reason for fk in expected
        ):
            raise ValueError("bad_faith reason 必须点名具体 field_key(#157 决议 §4)")
        scores = {d.field_key: d.score for d in result.dimensions}
        card_total_zero = bool(scores) and all(s == 0 for s in scores.values())
        card = {
            "schema": CARD_SCHEMA_VERSION,
            "resume_id": state["resume_id"],
            "cycle_id": state["cycle_id"],
            "dimensions": [d.model_dump() for d in result.dimensions],
            "attitude": result.attitude.model_dump(),
            "total": weighted_total(scores, state.get("weights", {})),
            # AI 全 0 = 初筛不过同样落 hard_zero(B2 评审 P1:0 分队列靠它捞)
            "hard_zero": card_total_zero or result.attitude.verdict == "bad_faith",
            "hard_zero_reasons": (
                {"_attitude": "AI 判定各维全 0,初筛不过"} if card_total_zero else {}
            ),
            "versions": {
                "prompt": _prompt_version(),
                "weights": "cycle-config",
                "model": settings.model_strong,
            },
        }
        return {"card": card, "error": None, "llm_usage": llm_usage}
    except Exception as exc:  # noqa: BLE001 — 失败进 error 态,B2 任务可重试
        return {"card": None, "error": f"{type(exc).__name__}: {exc}"}


async def finalize(state: EvaluationState) -> dict:
    """透传到终态(卡已在 llm_score 组装;单节点占位便于 B2 挂钩/审计)。"""
    return {}


_compiled: Any | None = None


def build_evaluation_subgraph() -> Any:
    """评分子图:START → precheck →(绝对卡? finalize_hard : llm_score)→ finalize → END。"""
    global _compiled
    if _compiled is not None:
        return _compiled
    g = StateGraph(EvaluationState)
    g.add_node("precheck", precheck)
    g.add_node("llm_score", llm_score)
    g.add_node("finalize_hard", finalize_hard)
    g.add_node("finalize", finalize)
    g.set_entry_point("precheck")
    g.add_conditional_edges("precheck", route_after_precheck)
    g.add_edge("llm_score", "finalize")
    g.add_edge("finalize_hard", "finalize")
    g.add_edge("finalize", END)
    _compiled = g.compile()
    return _compiled


async def run_evaluation(
    fields: list[dict[str, str]],
    *,
    resume_id: int,
    cycle_id: int,
    weights: dict[str, float] | None = None,
    usage_out: dict[str, int | None] | None = None,
    correlation_id: str | None = None,
) -> dict:
    """便捷入口:跑完整子图,返回卡 dict;LLM 失败抛 RuntimeError(B2 落 job 失败)。

    #183:usage_out 给定时回填评分模型 token 用量;correlation_id 给定时
    挂 Langfuse callbacks 并以 metadata.correlation_id 关联 trace(评测线
    trace 面此前未接线,配置了也不产生 trace——评审 P2 修正)。"""
    from official_agent.observability import langfuse_callbacks

    graph = build_evaluation_subgraph()
    config: dict[str, Any] = {}
    callbacks = langfuse_callbacks()
    if callbacks:
        config["callbacks"] = callbacks
    if correlation_id:
        config["metadata"] = {"correlation_id": correlation_id}
    final: EvaluationState = await graph.ainvoke(
        {
            "resume_id": resume_id,
            "cycle_id": cycle_id,
            "fields": fields,
            "weights": weights or {},
        },
        config=config,
    )
    if final.get("error") or not final.get("card"):
        raise RuntimeError(f"评分子图失败:{final.get('error')}")
    if usage_out is not None:
        usage_out.update(final.get("llm_usage") or {})
    return final["card"]
