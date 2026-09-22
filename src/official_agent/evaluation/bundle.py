"""证据线组装(评测错因/奖项/兜底)→ 统一题集信封。

与仓深挖线平行的一条「调查 bundle」:一次跑完所有证据线,产出一个
qbank 信封(groups 分线,source 汇总)。题数硬校验纪律与仓线深挖一致。
"""

from __future__ import annotations

import logging
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
from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable
from official_agent.evaluation.graph import _extract_json
from official_agent.evaluation.investigate import extract_repos
from official_agent.evaluation.schema import QuestionSet
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt, load_prompt_meta
from official_agent.security.injection_guard import wrap_data_zone

PROMPT_FILE = "evaluation/b4.md"

#: 仓线上限:一个仓一条完整探索子图(80 轮 / 300s 墙钟),而仓号来自候选人
#: 自填的项目栏——不封顶就等于把成本开关交给候选人(贴 50 个链接即 50 次探索)。
#: 取 5 与同文件既有风格一致(奖项 [:3]、技术名词 MAX_ITEMS=6)。
MAX_REPO_LINES = 5


def _prompt_version() -> str:
    return load_prompt_meta(PROMPT_FILE).get("version", "unknown")


def _project_text(fields: list) -> str:
    for f in fields:
        key = f.field_key.lower()
        if "project" in key or "项目" in f.title:
            return f.value
    return ""


def _resume_text(fields: list) -> str:
    """简历全文(各字段带标题拼接)。

    技术栈常单列一栏,只喂项目字段会让无仓路径看不见它——那正是这条路径
    最可靠的出题依据。

    字段面与评分线完全同一份(evaluation runner 在 job 入口已过
    mask_pii_deep),本函数不额外处理也不扩大暴露面:它只把同一批已脱敏字段
    按标题拼起来。脱敏规则的覆盖面由 security/pii.py 决定,不在此处重复。
    """
    parts = [f"{f.title or f.field_key}:\n{f.value}" for f in fields if (f.value or "").strip()]
    return "\n\n".join(parts)


def _group_kind(envelope: dict[str, Any]) -> str:
    """组标识:走了简历深挖为 cv_dive,其余(含带仓深挖)为 repo。

    按信封的 mode 判而不是「有没有钉仓」:仓读不到时会回退到简历深挖,那时
    虽然钉着仓号,内容其实是简历线。guided/skipped 沿用既有 repo——改它们的
    取值会波及前端标签映射与 qbank.source 语义,不在本票范围。
    """
    return "cv_dive" if envelope.get("mode") == "cv_dive" else "repo"


async def _b4_questions(
    payload_hint: str, *, count_min: int, count_max: int, source: str = "candidate-material"
) -> list[dict]:
    """共用的提示词 JSON 出题(错因追问/技能题组)。

    材料来自候选人(简历字段/评测记录),是不可信输入,一律包数据区——裸拼
    会让简历里一句「忽略以上要求」以指令级身份落进 prompt(与出题轨、评分轨
    同一条红线)。
    """
    settings = get_effective_settings()
    model = build_model(settings, temperature=0.2)
    prompt_text = (
        load_prompt(PROMPT_FILE)
        + "\n\n---\n\n材料:\n"
        + wrap_data_zone(source, payload_hint)
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
        raise ValueError(f"题数越界:{len(questions)}")
    return questions


def _group_has_evidence(g: dict[str, Any]) -> bool:
    """该组是否带真实题目(repo 组已 v2:题目在 entry/chains 里,两种形状都认)。"""
    qbank_v2 = g.get("qbank_v2")
    if isinstance(qbank_v2, dict):
        inner = qbank_v2.get("group") or {}
        return bool(inner.get("entry") or inner.get("chains"))
    return bool(g.get("questions"))


async def _repo_lines(
    project_text: str, *, resume_id: int, github_token: str
) -> list[tuple[str, str] | None]:
    """仓线分线:可读的仓各一条线,读不到的合起来只给一条(钉第一个)。

    可读性在这里一次探清,而不是让每个仓各跑一遍子图、各自发现读不到:
    仓读不到时材料只剩简历自述,而自述只有一份——逐个仓跑只会得到一堆
    一模一样的简历题。无仓/有项目文本但无仓 URL → 仍跑一次([None]),
    让子图内部按简历内容路由。
    """
    repo_candidates = extract_repos(project_text)
    if len(repo_candidates) > MAX_REPO_LINES:
        logging.getLogger(__name__).warning(
            "bundle_repo_cap resume_id=%s total=%d kept=%d dropped=%r",
            resume_id,
            len(repo_candidates),
            MAX_REPO_LINES,
            [f"{o}/{n}" for o, n in repo_candidates[MAX_REPO_LINES:]][:5],
        )
        repo_candidates = repo_candidates[:MAX_REPO_LINES]
    if not repo_candidates:
        return [None]
    probe = GitHubClient(token=github_token)
    readable: list[tuple[str, str]] = []
    unreadable: list[tuple[str, str]] = []
    for owner, name in repo_candidates:
        try:
            await probe.repo(owner, name)
        except GitHubUnavailable:
            unreadable.append((owner, name))
        else:
            readable.append((owner, name))
    return [*readable, *unreadable[:1]]


async def _repo_line(
    pinned: tuple[str, str] | None,
    *,
    project_text: str,
    resume_text: str,
    github_key: str | None,
    github_token: str,
    grade: str,
    cv_dive_done: bool,
) -> tuple[dict[str, Any], bool]:
    """跑一个仓的调查子图,返回 (组, 本线是否走了简历深挖)。"""
    owner, name = pinned if pinned else ("", "")
    try:
        repo_envelope = await ig.run_investigation(
            project_text,
            resume_text=resume_text,
            repo=pinned,
            github_token=github_token,
            candidate_login=github_key or "",
            grade=grade,
            cv_dive_done=cv_dive_done,
        )
    except Exception as exc:  # noqa: BLE001 — 单线失败降级为错误组,不炸 bundle
        # 仓线失败必须留痕:信封 error 字段只有截断摘要,没有堆栈(对照评测线)
        logging.getLogger(__name__).warning(
            "仓线探测失败 repo=%s/%s", owner, name, exc_info=True
        )
        return (
            {
                "group": "repo",
                "owner": owner,
                "repo": f"{owner}/{name}" if pinned else "",
                "mode": "error",
                "repo_summary": "",
                "questions": [],
                "error": f"{type(exc).__name__}: {exc}"[:300],
            },
            False,
        )
    became_cv_dive = repo_envelope.get("mode") == "cv_dive"
    group = {
        "group": _group_kind(repo_envelope),
        "owner": owner,
        "repo": f"{owner}/{name}" if pinned else "",
        # v2 信封整体嵌套(qbank_v2),不展开——QbankV2 自带
        # group 键(dict),展开会覆盖 kind 字符串并污染 pick log
        "qbank_v2": repo_envelope,
    }
    return group, became_cv_dive


async def _autograding_line(github_key: str, resume_id: int) -> dict[str, Any] | None:
    """评测线:有评测记录且非满分 → 失败 test 错因追问;失败返回 None。"""
    if not github_key:
        return None
    try:
        from official_agent.evaluation import autograding as ag

        submission = await ag.fetch_latest_submission(github_key)
        if not submission or ag.is_full_score(submission):
            return None
        failures = ag.extract_failures(submission)
        if not failures:
            return None  # 非满分但抽不出失败明细:出题只会诱导编造,略过该线
        buckets = ag.classify(failures)
        material = "\n".join(
            f"[{kind}] {task}/{name}"
            for kind, items in buckets.items()
            for task, name in items
        )
        ag_questions = await _b4_questions(
            f"评测失败清单(任务/test):\n{material}",
            count_min=2,
            count_max=3,
            source="autograding-failures",
        )
        return {
            "group": "autograding",
            "mode": "error_analysis",
            "repo_summary": f"评测非满分,失败 {len(failures)} 项",
            "questions": ag_questions,
            "prompt_version": _prompt_version(),
        }
    except Exception as exc:  # noqa: BLE001 — 错因线失败可容忍,只记日志
        logging.getLogger(__name__).warning(
            "评测错因线跳过(resume=%s):%s: %s", resume_id, type(exc).__name__, exc
        )
        return None


async def _awards_line(provider: Any, fields: list, resume_id: int) -> dict[str, Any] | None:
    """奖项线:verified 的背景卡也进信封(搜索通道到位后面试官有料可读)。

    单个奖项的取材失败跳过该奖项,不拖垮其余奖项与其他线。
    """
    awards = extract_awards(fields)
    award_questions: list[dict] = []
    award_briefs: list[dict] = []
    for title in awards[:3]:
        try:
            brief = await build_award_brief(provider, title)
        except Exception as exc:  # noqa: BLE001 — 单奖项失败可容忍,只记日志
            logging.getLogger(__name__).warning(
                "奖项线取材跳过(resume=%s,奖项=%s):%s: %s",
                resume_id,
                title,
                type(exc).__name__,
                exc,
            )
            continue
        award_briefs.append(brief)
        award_questions.extend(brief.get("questions", []))
    if not (award_questions or award_briefs):
        return None
    return {
        "group": "awards",
        "mode": "award_brief",
        "repo_summary": f"奖项 {len(awards)} 项",
        "questions": award_questions,
        "briefs": award_briefs,
        "prompt_version": _prompt_version(),
    }


async def _fallback_line(fields: list) -> dict[str, Any]:
    """兜底线:仓线无题、无评测、无奖项 → 基础三维 + 部门技能题组。"""
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
    return {
        "group": "base_and_skills",
        "mode": "base_fallback",
        "repo_summary": "无证据候选,基础三维+部门技能题组",
        "questions": base_qs + skill_qs,
        "prompt_version": _prompt_version(),
    }


def _flatten_v2_questions(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """v2 信封展平成扁平题表(entry/chains/reserves;time_minutes 供 suggest_plan)。"""
    all_questions: list[dict[str, Any]] = []
    for g in groups:
        qbank_v2 = g.get("qbank_v2")
        if not isinstance(qbank_v2, dict):
            all_questions.extend(g.get("questions", []))
            continue
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
    return all_questions


def _aggregate_usage(groups: list[dict[str, Any]]) -> dict[str, int | None]:
    """聚合 repo 组探索/出题用量(其余线 v1 形状无用量面)。"""
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
    return usage_total


async def run_bundle(
    fields: list,
    *,
    resume_id: int,
    cycle_id: int,
    github_key: str | None = None,
    github_token: str = "",
    search_provider: Any | None = None,
    grade: str = "",
) -> dict[str, Any]:
    """跑全部证据线,返回 qbank 信封(groups 分线+15 分钟建议组合)。

    - 仓线:调查子图(deep/cv/guided/skip 内部自决);仓读不到时回退简历深挖,
      而简历深挖全局只出一条线
    - 评测线:有评测记录且非满分 → 失败 test 错因追问
    - 奖项线:简历有奖项 → 背景卡+纯过程追问;搜索通道不可用 → 不可考
    - 兜底线:以上全无 → 基础三维 + 部门技能题组

    grade 是**元信息**(年级原文):只透传给调查子图调节出题深度,不参与
    任何评分线;缺省空串即「年级缺失」,子图走安全默认。
    """
    provider = search_provider or NullSearchProvider()
    groups: list[dict[str, Any]] = []
    project_text = _project_text(fields)
    resume_text = _resume_text(fields)

    # 仓线。多仓:项目文本里每个 GitHub 仓各深挖一次,产出独立 repo group;
    # 单线失败降级为空错误组,不炸整条 bundle。简历深挖全局只出一条线,
    # 由分线保证;cv_dive_done 是兜底:可读的仓若在子图内探测时恰好也读不到
    # (限流等瞬时故障),它会回退到简历深挖,而那时简历线不该再挖第二遍。
    cv_dive_done = False
    for pinned in await _repo_lines(project_text, resume_id=resume_id, github_token=github_token):
        group, became_cv_dive = await _repo_line(
            pinned,
            project_text=project_text,
            resume_text=resume_text,
            github_key=github_key,
            github_token=github_token,
            grade=grade,
            cv_dive_done=cv_dive_done,
        )
        cv_dive_done = cv_dive_done or became_cv_dive
        groups.append(group)

    autograding_group = await _autograding_line(github_key, resume_id)
    if autograding_group is not None:
        groups.append(autograding_group)

    awards_group = await _awards_line(provider, fields, resume_id)
    if awards_group is not None:
        groups.append(awards_group)

    if not any(_group_has_evidence(g) for g in groups):
        groups.append(await _fallback_line(fields))

    all_questions = _flatten_v2_questions(groups)
    return {
        "schema_name": "evaluation_qbank/v2",
        "groups": groups,
        "suggested_plan": suggest_plan(all_questions, budget_minutes=15),
        "total_questions": len(all_questions),
        "explore_usage_total": _aggregate_usage(groups),
        "prompt_version": _prompt_version(),
    }
