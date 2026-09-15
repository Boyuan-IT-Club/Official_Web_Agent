"""CV 深挖出题报告 executor:真实简历跑真实出题图,产出供**人工审批**的报告。

与其余 suite 的区别:这里**不设门禁、不做断言**。「题目问得好不好」本质主观,
自动断言会把人引到错误的优化目标上。本套件只做两件事:

1. 跑真实图(真实 LLM、真实出题链路,不 mock),把每一环的可观测口径记下来
   —— 路由结果、抽出几个技术栈、出题数与题组结构 —— 用于**定位是哪一环退化**;
2. 把题目全文落成可读报告,交给人去读。

文件级套件(per-suite)恒 PASS:人工审批不进退出码。但**环境未配置或样本不存在
必须走 SKIP**,绝不静默变绿 —— 那会让人以为跑过了。

数据纪律(红线):
- 报告逐字引用简历原文(防编造设计使然:题面锚在原文句上),**落盘前必须过
  ``mask_pii_deep``**。脱敏复用既有实现,此处不新写规则。
- 真实简历样本自身永不进仓库(gitignore);报告落在同样被忽略的目录下。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult

#: 真实简历样本目录(相对仓库根;gitignore)。env_blocker 与执行体共用。
_RESUME_DIR = Path("evals/real_resumes")
#: 报告落盘目录(同样 gitignore,因为报告含简历原文片段)
_REPORT_DIR = Path("evals/reports")


def _llm_blocker() -> str | None:
    """真实 LLM 驱动:无 key 则 SKIP(与其余 LLM 类套件同一判据)。"""
    from official_agent.evals.tool_selection import env_blocker as _ts_blocker

    return _ts_blocker()


def _resumes() -> list[Path]:
    if not _RESUME_DIR.is_dir():
        return []
    return sorted(p for p in _RESUME_DIR.glob("*.txt") if p.is_file())


def env_blocker() -> str | None:
    """缺 LLM 配置或没有样本 → 报明原因让引擎标 SKIP。

    「样本不存在」也算环境未配置:套件是给有样本的人在手边跑的,没样本时
    不能谎报通过。
    """
    blocker = _llm_blocker()
    if blocker:
        return blocker
    if not _resumes():
        return f"没有真实简历样本:{_RESUME_DIR}/ 为空(样本含 PII,不入库,需本地放置)"
    return None


def _write(path: Path, text: str) -> None:
    """同步写盘(ASYNC240:异步体内不做阻塞 IO,交给线程池)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mask(payload: Any) -> Any:
    """报告出边界的唯一脱敏口:复用既有深层脱敏(不要另写规则)。"""
    from official_agent.security.pii import mask_pii_deep

    return mask_pii_deep(payload)


def _resume_body(path: Path) -> str:
    """简历正文:样本文件里 ``=====`` 之后是人工批注,不是简历内容,须去掉。

    批注是给人看的笔记,把它喂进图等于用标注污染被测对象。
    """
    text = path.read_text(encoding="utf-8")
    return text.split("=====", 1)[0].strip()


def _survey(envelope: dict[str, Any]) -> dict[str, int]:
    """题组结构计数(定位退化的可观测口径)。"""
    group = envelope.get("group") or {}
    chains = group.get("chains") or []
    reserves = group.get("reserves") or []
    layers = sum(len(c.get("layers") or []) for c in chains)
    three_tier = sum(
        1
        for c in chains
        for layer in (c.get("layers") or [])
        if layer.get("answer_reference")
    )
    return {
        "chains": len(chains),
        "chain_layers": layers,
        "reserves": len(reserves),
        "entry": 1 if group.get("entry") else 0,
        "total_questions": (1 if group.get("entry") else 0) + layers + len(reserves),
        "layers_with_reference": three_tier,
    }


def _render_reference(ref: dict[str, Any] | None, indent: str) -> list[str]:
    """三档参考答案 → Markdown 行(面试官凭它判定答得好不好)。"""
    if not ref:
        return []
    return [
        f"{indent}- {tier}:{ref[tier]}"
        for tier in ("strong", "acceptable", "weak")
        if ref.get(tier)
    ]


def _render_group(group: dict[str, Any]) -> list[str]:
    """题组 → Markdown 行(题目全文,供人阅读)。"""
    lines: list[str] = []
    entry = group.get("entry")
    if entry:
        lines.append(f"#### 入口 · {entry.get('category', '')}")
        lines.append(f"> {entry.get('question', '')}")
        lines += _render_reference(entry.get("answer_reference"), "  ")
        note = (entry.get("evidence") or {}).get("note", "")
        if note:
            lines.append(f"  - 出处:{note}")
        lines.append("")
    for ci, chain in enumerate(group.get("chains") or [], 1):
        lines.append(f"#### 链{ci} · {chain.get('category', '')} · 主题:`{chain.get('theme', '')}`")
        for li, layer in enumerate(chain.get("layers") or [], 1):
            lines.append(f"{li}. {layer.get('question', '')}")
            if layer.get("expected_signal"):
                lines.append(f"   - 过关信号:{layer['expected_signal']}")
            lines += _render_reference(layer.get("answer_reference"), "   ")
        lines.append("")
    reserves = group.get("reserves") or []
    if reserves:
        lines.append("#### 备选")
        for ri, r in enumerate(reserves, 1):
            lines.append(f"{ri}. [{r.get('category', '')}] {r.get('question', '')}")
            lines += _render_reference(r.get("answer_reference"), "   ")
        lines.append("")
    return lines


async def _one(path: Path, index: int) -> tuple[CaseResult, list[str]]:
    """跑一份简历,返回(用例结果, 报告行)。"""
    from official_agent.evaluation import investigate_graph as ig
    from official_agent.evaluation.tech_stack import extract_tech_stack

    body = _resume_body(path)
    if not body:
        return CaseResult(id=path.name, passed=False, detail="样本为空"), []

    # 技术栈单独抽一次:它既是路由输入,也是报告里「抽出几个」的口径
    try:
        tech_items = await extract_tech_stack(body)
    except Exception as exc:  # noqa: BLE001 — 报告模式:抽取失败不应中止整轮
        tech_items = []
        tech_note = f"(技术栈抽取失败:{type(exc).__name__})"
    else:
        tech_note = ""

    tech_line = f"- 抽出技术栈:**{len(tech_items)}** 项{tech_note}"

    try:
        envelope = await ig.run_investigation(body, resume_text=body)
    except Exception as exc:  # noqa: BLE001 — 单份失败照实记,不炸整轮
        # 报告是给人定位退化的,失败原因必须落到正文:只写异常类型等于让人
        # 再跑一遍才知道挂在哪一环。
        reason = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
        return (
            CaseResult(
                id=path.name,
                passed=False,
                detail=f"出题失败:{reason[:80]}",
            ),
            [
                f"### {index}. `{path.name}`",
                "",
                "- 路由:**(未走到)**",
                tech_line,
                "- 出题:**失败**",
                "",
                f"```\n{reason}\n```",
                "",
            ],
        )

    survey = _survey(envelope)
    lines = [
        f"### {index}. `{path.name}`",
        "",
        f"- 路由:**{envelope.get('mode')}**",
        tech_line,
        f"- 出题:**{survey['total_questions']}** 道"
        f"(入口 {survey['entry']} · 链 {survey['chains']} 条/{survey['chain_layers']} 层"
        f" · 备选 {survey['reserves']})",
        f"- 链层带三档答案:**{survey['layers_with_reference']}/{survey['chain_layers']}**",
        f"- prompt 版本:`{envelope.get('prompt_version', '')}`",
        "",
    ]
    if tech_items:
        names = "、".join(f"`{t.name}`({t.claimed_level})" for t in tech_items)
        lines += [f"- 技术栈:{names}", ""]
    lines += _render_group(envelope.get("group") or {})
    detail = (
        f"mode={envelope.get('mode')} tech={len(tech_items)} "
        f"题={survey['total_questions']} 三档={survey['layers_with_reference']}"
    )
    return CaseResult(id=path.name, passed=True, detail=detail), lines


async def run_suite(path: Path, *, distribution: bool = False, **_: Any) -> SuiteResult:
    """跑全部样本并落一份人可读报告;自身恒 PASS(人工审批不进退出码)。

    每份简历是一条 case,便于在汇总行里一眼看到总量与失败项;**不设通过门**。
    """
    del distribution  # 报告模式:无分布口径
    resumes = _resumes()
    cases: list[CaseResult] = []
    sections: list[str] = []

    for index, resume in enumerate(resumes, 1):
        case, lines = await _one(resume, index)
        cases.append(case)
        sections += lines

    header = [
        "# CV 深挖出题报告(供人工审批)",
        "",
        "> 本报告由 `evals/datasets/cv_dive_report.yaml` 生成:真实简历跑真实出题图。",
        "> 题目「问得好不好」是主观判断,故本套件**不设门禁**,只供人阅读。",
        "> 报告落盘前已过 `mask_pii_deep`(题面逐字引用简历原文,不掩码会带出 PII)。",
        "",
        f"样本数:**{len(resumes)}** · 数据源:`{_RESUME_DIR}/`(gitignore,不入库)",
        "",
        "---",
        "",
    ]
    body = _mask(sections)

    report_path = _REPORT_DIR / "cv_dive_report.md"
    await asyncio.to_thread(_write, report_path, "\n".join(header + body))

    # 机读口径另落一份(汇总用)。同样必须过掩码:失败分支的 detail 会带上
    # 校验异常原文,而异常原文里可能内嵌简历内容(如链主题/题面片段),
    # 不能只在 markdown 那条路径上脱敏。
    summary_path = _REPORT_DIR / "cv_dive_report.json"
    await asyncio.to_thread(
        _write,
        summary_path,
        json.dumps(
            _mask(
                {
                    "samples": len(resumes),
                    "cases": [
                        {"id": c.id, "passed": c.passed, "detail": c.detail} for c in cases
                    ],
                }
            ),
            ensure_ascii=False,
            indent=2,
        ),
    )

    return SuiteResult(
        name=path.stem,
        kind="cv_dive_report",
        source=path.name,
        status="PASS",  # 报告模式:人工审批不进退出码
        cases=cases,
        metrics={"samples": float(len(resumes))},
        notes=[
            f"报告 → {report_path}(人工审批用;题目全文与路由/技术栈/题数口径)",
            "本套件不设门禁:出题质量为主观判断,自动断言校准前不进 CI",
        ],
    )
