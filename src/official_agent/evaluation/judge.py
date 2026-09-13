"""出题质量 LLM-as-judge(#155/#62):dossier+题组 → 四维 1-5 分报告。

首版**只报告不阻塞**(报告落 evals/last_qbank_judge_report.json;阈值等
AG8 #156 校准后转门禁)。judge prompt 走 prompts/ 版本化(ADR-0004)。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage

from official_agent.evaluation.graph import _extract_json
from official_agent.evaluation.schema import JudgeReport
from official_agent.prompt_loader import load_prompt, load_prompt_meta

PROMPT_FILE = "evaluation/judge.md"


def judge_prompt_version() -> str:
    return load_prompt_meta(PROMPT_FILE).get("version", "unknown")


async def judge_qbank(
    dossier_text: str,
    group_payload: dict[str, Any],
    *,
    model: Any,
) -> dict[str, Any]:
    """评一个题组:返回 JudgeReport dump(四维 1-5 分+理由+总评)。"""
    prompt_text = (
        load_prompt(PROMPT_FILE)
        + "\n\n---\n\n探索材料 dossier:\n"
        + dossier_text
        + "\n\n题组 JSON:\n"
        + json.dumps(group_payload, ensure_ascii=False)
    )
    resp = await model.ainvoke([HumanMessage(content=prompt_text)])
    raw = resp.content
    if isinstance(raw, list):
        raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    text = raw if isinstance(raw, str) else str(raw)
    report = JudgeReport.model_validate_json(_extract_json(text))
    out = report.model_dump()
    out["prompt_version"] = judge_prompt_version()
    return out
