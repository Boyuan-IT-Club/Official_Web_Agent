"""B4 证据线组装(#131/#132/#133):评测错因/奖项/兜底 → 统一题集信封。

与 B3 仓深挖平行的一条「调查 bundle」:一次跑完所有证据线,产出一个
qbank 信封(groups 分线,source 汇总)。题数硬校验纪律同 B3。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from official_agent.config import get_effective_settings
from official_agent.evaluation import investigate_graph as ig
from official_agent.evaluation.awards import (
    NullSearchProvider,
    base_three_questions,
    build_award_brief,
    extract_awards,
    suggest_plan,
)
from official_agent.evaluation.graph import _extract_json
from official_agent.evaluation.investigate import extract_repos
from official_agent.evaluation.schema import QuestionSet
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt, load_prompt_meta

PROMPT_FILE = "evaluation/b4.md"


def _prompt_version() -> str:
    return load_prompt_meta(PROMPT_FILE).get("version", "unknown")


def _project_text(fields: list) -> str:
    for f in fields:
        key = f.field_key.lower()
        if "project" in key or "项目" in f.title:
            return f.value
    return ""


async def _b4_questions(payload_hint: str, *, count_min: int, count_max: int) -> list[dict]:
    """B4 共用的提示词 JSON 出题(错因追问/技能题组)。"""
    settings = get_effective_settings()
    model = build_model(settings, temperature=0.2)
    prompt_text = (
        load_prompt(PROMPT_FILE)
        + "\n\n---\n\n材料:\n"
        + payload_hint
        + f"\n\n出 {count_min}-{count_max} 道题。"
        + "\n只输出符合 schema 的 JSON 对象,不要任何其他文字或代码围栏。"
    )
    resp = await model.ainvoke([HumanMessage(content=prompt_text)])
    raw = resp.content
    if isinstance(raw, list):
        raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
    content = raw if isinstance(raw, str) else str(raw)
    qs = QuestionSet.model_validate_json(_extract_json(content))
    questions = [q.model_dump() for q in qs.questions]
    if not count_min <= len(questions) <= count_max:
        raise ValueError(f"B4 题数越界:{len(questions)}")
    return questions


async def run_bundle(
    fields: list,
    *,
    resume_id: int,
    cycle_id: int,
    github_key: str | None = None,
    github_token: str = "",
    search_provider: Any | None = None,
) -> dict[str, Any]:
    """跑全部证据线,返回 qbank 信封(groups 分线+15 分钟建议组合)。

    - 仓线:B3 子图(deep/guided/skip 内部自决)
    - 评测线:有评测记录且非满分 → 失败 test 错因追问(#132)
    - 奖项线:简历有奖项 → 背景卡+纯过程追问;搜索不可用(检查点⑤)→ 不可考
    - 兜底线(#133):以上全无 → 基础三维 + 部门技能题组
    """
    provider = search_provider or NullSearchProvider()
    groups: list[dict[str, Any]] = []
    project_text = _project_text(fields)

    # 仓线(B3)。M-1 多仓:项目文本里每个 GitHub 仓各深挖一次,产出独立
    # repo group(带 owner/repo 标识),不再只挖第一个。单线失败降级为空错误组,
    # 不炸整条 bundle(B4 评审 P2)。
    repo_candidates = extract_repos(project_text)
    # 无仓/有项目文本但无仓 URL → 仍跑一次,让子图内部路由到 guided/skip
    if not repo_candidates:
        repo_candidates = [(None, None)]  # type: ignore[list-item]
    for owner, name in repo_candidates:
        pinned = (owner, name) if owner and name else None
        try:
            repo_envelope = await ig.run_investigation(
                project_text,
                repo=pinned,
                github_token=github_token,
                candidate_login=github_key or "",
            )
            groups.append(
                {
                    "group": "repo",
                    "owner": owner or "",
                    "repo": f"{owner}/{name}" if pinned else "",
                    # #153:v2 信封整体嵌套(qbank_v2),不展开——QbankV2 自带
                    # group 键(dict),展开会覆盖 kind 字符串并污染 pick log
                    "qbank_v2": repo_envelope,
                }
            )
        except Exception as exc:  # noqa: BLE001
            groups.append(
                {
                    "group": "repo",
                    "owner": owner or "",
                    "repo": f"{owner}/{name}" if pinned else "",
                    "mode": "error",
                    "repo_summary": "",
                    "questions": [],
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            )

    # 评测线(#132)
    if github_key:
        from official_agent.evaluation import autograding as ag

        submission = await ag.fetch_latest_submission(github_key)
        if submission and not ag.is_full_score(submission):
            failures = ag.extract_failures(submission)
            if not failures:
                pass  # 非满分但抽不出失败明细:出题只会诱导编造,略过该线
            else:
                buckets = ag.classify(failures)
                material = "\n".join(
                    f"[{kind}] {task}/{name}"
                    for kind, items in buckets.items()
                    for task, name in items
                )
                ag_questions = await _b4_questions(
                    f"评测失败清单(任务/test):\n{material}", count_min=2, count_max=3
                )
                groups.append(
                    {
                        "group": "autograding",
                        "mode": "error_analysis",
                        "repo_summary": f"评测非满分,失败 {len(failures)} 项",
                        "questions": ag_questions,
                        "prompt_version": _prompt_version(),
                    }
                )

    # 奖项线(#131):verified 的背景卡也进信封(检查点⑤到位后面试官有料可读)
    awards = extract_awards(fields)
    award_questions: list[dict] = []
    award_briefs: list[dict] = []
    for title in awards[:3]:
        brief = await build_award_brief(provider, title)
        award_briefs.append(brief)
        award_questions.extend(brief.get("questions", []))
    if award_questions or award_briefs:
        groups.append(
            {
                "group": "awards",
                "mode": "award_brief",
                "repo_summary": f"奖项 {len(awards)} 项",
                "questions": award_questions,
                "briefs": award_briefs,
                "prompt_version": _prompt_version(),
            }
        )

    # 兜底线(#133):仓线无题、无评测、无奖项 → 基础三维 + 部门技能题组。
    # #153:repo 组已 v2(题目在 group.entry/chains 里),证据判定两种形状都认
    def _group_has_evidence(g: dict[str, Any]) -> bool:
        qbank_v2 = g.get("qbank_v2")
        if isinstance(qbank_v2, dict):
            inner = qbank_v2.get("group") or {}
            return bool(inner.get("entry") or inner.get("chains"))
        return bool(g.get("questions"))

    evidence_present = any(_group_has_evidence(g) for g in groups)
    if not evidence_present:
        base_qs = base_three_questions()
        department = next(
            (f.value for f in fields if "dept" in f.field_key.lower() or "部门" in f.title),
            "",
        )
        skill_qs = await _b4_questions(
            f"候选部门:{department or '未填'}。候选人材料:\n" + "\n".join(f.value for f in fields),
            count_min=2,
            count_max=4,
        )
        groups.append(
            {
                "group": "base_and_skills",
                "mode": "base_fallback",
                "repo_summary": "无证据候选,基础三维+部门技能题组",
                "questions": base_qs + skill_qs,
                "prompt_version": _prompt_version(),
            }
        )

    all_questions: list[dict[str, Any]] = []
    for g in groups:
        qbank_v2 = g.get("qbank_v2")
        if isinstance(qbank_v2, dict):
            # repo v2 组:题目收集为 dict(带 time_minutes,suggest_plan 需要)
            inner = qbank_v2.get("group") or {}
            entry = inner.get("entry")
            if entry:
                all_questions.append(
                    {
                        "question": entry.get("question", ""),
                        "time_minutes": entry.get("time_minutes", 3),
                    }
                )
            for chain in inner.get("chains", []):
                for layer in chain.get("layers", []):
                    all_questions.append({"question": layer.get("question", ""), "time_minutes": 3})
            for reserve in inner.get("reserves", []):
                all_questions.append(
                    {
                        "question": reserve.get("question", ""),
                        "time_minutes": reserve.get("time_minutes", 3),
                    }
                )
        else:
            all_questions.extend(g.get("questions", []))
    # D9/#154:聚合 repo 组探索/出题用量(其余线 v1 形状无用量面)
    usage_total: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
    }
    for g in groups:
        qbank_v2 = g.get("qbank_v2") or {}
        metas = [qbank_v2.get("explore_meta") or {}, qbank_v2.get("generation_usage") or {}]
        for meta in metas:
            for k in usage_total:
                v = meta.get(k)
                if v is not None:
                    usage_total[k] = (usage_total[k] or 0) + int(v)
    envelope = {
        "schema_name": "evaluation_qbank/v2",
        "groups": groups,
        "suggested_plan": suggest_plan(all_questions, budget_minutes=15),
        "total_questions": len(all_questions),
        "explore_usage_total": usage_total,
        "prompt_version": _prompt_version(),
    }
    return envelope
