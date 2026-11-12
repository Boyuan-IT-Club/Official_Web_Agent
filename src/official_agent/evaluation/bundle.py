"""证据线组装(评测错因/奖项/兜底)→ 统一题集信封。

与仓深挖线平行的一条「调查 bundle」:一次跑完所有证据线,产出一个
qbank 信封(groups 分线,source 汇总)。题数硬校验纪律与仓线深挖一致。

并发约定:仓线、评测线、奖项线同时跑,线与线之间不共享可变状态——每条线
自带材料与模型上下文,只把自己的组交回汇合处。跨线的约束(简历深挖全局只留
一条、无证据才兜底)全部在汇合处由代码裁决,所以信封里组的顺序与各线谁先
跑完无关。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
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
from official_agent.evaluation.llm_common import (
    TEMPERATURE_GENERATION,
    content_text,
    gated_ainvoke,
    prompt_version,
)
from official_agent.evaluation.plan import LinePlan, Plan, RepoCandidate, make_plan
from official_agent.evaluation.question_review import (
    review_line,
    review_mode,
    targets_from_questions,
)
from official_agent.evaluation.schema import QuestionSet
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt
from official_agent.security.injection_guard import wrap_data_zone

PROMPT_FILE = "evaluation/b4.md"

#: 仓线上限:一个仓一条完整探索子图(80 轮 / 300s 墙钟),而仓号来自候选人
#: 自填的项目栏——不封顶就等于把成本开关交给候选人(贴 50 个链接即 50 次探索)。
#: 取 5 与同文件既有风格一致(奖项 [:3]、技术名词 MAX_ITEMS=6)。
MAX_REPO_LINES = 5

#: 一个 bundle 里同时在跑的仓线上限。每条仓线同一时刻至多一次模型调用、一个
#: GitHub 请求,所以这个数就是单个 bundle 在仓线上的峰值并发;全部放开等于让
#: 候选人贴了几个链接来决定同时起几个探索者。取 3:封顶的 5 条线两批跑完。
MAX_PARALLEL_REPO_LINES = 3


def _prompt_version() -> str:
    return prompt_version(PROMPT_FILE)


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
    model = build_model(settings, temperature=TEMPERATURE_GENERATION)
    prompt_text = (
        load_prompt(PROMPT_FILE)
        + "\n\n---\n\n材料:\n"
        + wrap_data_zone(source, payload_hint)
        + f"\n\n出 {count_min}-{count_max} 道题。"
        + "\n只输出符合 schema 的 JSON 对象,不要任何其他文字或代码围栏。"
    )
    resp = await gated_ainvoke(model, [HumanMessage(content=prompt_text)])
    qs = QuestionSet.model_validate_json(_extract_json(content_text(resp)))
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
) -> tuple[list[tuple[str, str] | None], dict[tuple[str, str], dict[str, Any]]]:
    """仓线分线:可读的仓各一条线,读不到的合起来只给一条(钉第一个)。

    返回 (各线钉住的仓, 可读仓的 GitHub 元数据)。元数据就是探测时拿到的那份,
    留给规划段用,不为它多发请求。

    可读性在这里一次探清,而不是让每个仓各跑一遍子图、各自发现读不到:
    仓读不到时材料只剩简历自述,而自述只有一份——逐个仓跑只会得到一堆
    一模一样的简历题。无仓/有项目文本但无仓 URL → 仍跑一次([None]),
    让子图内部按简历内容路由。

    各仓同时探(并发度由 GitHubClient 的请求闸封顶),结果仍按简历里的出现序
    排:GitHub 整体不可达时每个探测都要等满超时,逐个探就是几倍的空等。
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
        return [None], {}
    probe = GitHubClient(token=github_token)

    async def _repo_meta(owner: str, name: str) -> dict[str, Any] | None:
        """仓可读返回元数据,读不到返回 None。"""
        try:
            return await probe.repo(owner, name)
        except GitHubUnavailable:
            return None

    metas = await asyncio.gather(*(_repo_meta(o, n) for o, n in repo_candidates))
    readable = {
        repo: meta for repo, meta in zip(repo_candidates, metas, strict=True) if meta is not None
    }
    unreadable = [repo for repo in repo_candidates if repo not in readable]
    return [*readable, *unreadable[:1]], readable


async def _repo_line(
    pinned: tuple[str, str] | None,
    *,
    project_text: str,
    resume_text: str,
    github_key: str | None,
    github_token: str,
    grade: str,
    line_plan: LinePlan | None = None,
) -> dict[str, Any]:
    """跑一个仓的调查子图,返回该线的组;失败降级为错误组,不向外抛。

    不告诉子图「别的线有没有挖过简历」:各线同时在跑,谁也不知道别的线会不会
    回退到简历深挖。重复的简历深挖组由汇合处裁掉(_keep_first_cv_dive)。
    line_plan 是这条线的探索预算与侧重;没有计划的线(没钉仓、仓读不到)不进
    探索段,用不上。
    """
    owner, name = pinned if pinned else ("", "")
    repo_label = f"{owner}/{name}" if pinned else ""
    try:
        repo_envelope = await ig.run_investigation(
            project_text,
            resume_text=resume_text,
            repo=pinned,
            github_token=github_token,
            candidate_login=github_key or "",
            grade=grade,
            explore_budget=line_plan.budget if line_plan else None,
            explore_focus=line_plan.focus if line_plan else (),
        )
        return {
            "group": _group_kind(repo_envelope),
            "owner": owner,
            "repo": repo_label,
            # v2 信封整体嵌套(qbank_v2),不展开——QbankV2 自带
            # group 键(dict),展开会覆盖 kind 字符串并污染 pick log
            "qbank_v2": repo_envelope,
        }
    except Exception as exc:  # noqa: BLE001 — 单线失败降级为错误组,不炸 bundle
        # 仓线失败必须留痕:信封 error 字段只有截断摘要,没有堆栈(对照评测线)
        logging.getLogger(__name__).warning(
            "仓线探测失败 repo=%s/%s", owner, name, exc_info=True
        )
        return {
            "group": "repo",
            "owner": owner,
            "repo": repo_label,
            "mode": "error",
            "repo_summary": "",
            "questions": [],
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


async def _repo_groups(
    pins: list[tuple[str, str] | None],
    *,
    project_text: str,
    resume_text: str,
    github_key: str | None,
    github_token: str,
    grade: str,
    plan: Plan,
) -> list[dict[str, Any]]:
    """各仓线并发跑,返回与 pins 同序的组(至多 MAX_PARALLEL_REPO_LINES 条同时在跑)。

    每条线是一次独立的子图调用,档案与消息历史都在线内:一个仓里的文本进不了
    另一条线的模型上下文,一条线失败也只换来它自己的错误组。
    """
    gate = asyncio.Semaphore(MAX_PARALLEL_REPO_LINES)

    async def _gated_line(pinned: tuple[str, str] | None) -> dict[str, Any]:
        async with gate:
            return await _repo_line(
                pinned,
                project_text=project_text,
                resume_text=resume_text,
                github_key=github_key,
                github_token=github_token,
                grade=grade,
                line_plan=plan.for_line(_line_id(pinned)),
            )

    return list(await asyncio.gather(*(_gated_line(pinned) for pinned in pins)))


def _line_id(pinned: tuple[str, str] | None) -> str:
    """钉住的仓 → 它在计划里的线号(owner/name);没钉仓为空串。"""
    return f"{pinned[0]}/{pinned[1]}" if pinned else ""


def _without_cut_lines(
    pins: list[tuple[str, str] | None], plan: Plan, *, resume_id: int
) -> list[tuple[str, str] | None]:
    """去掉被整包预算上限砍掉的线(留痕);其余的线顺序不变。"""
    cut = [line.line_id for line in plan.lines if line.is_dropped]
    if cut:
        logging.getLogger(__name__).warning(
            "bundle_plan_cut resume_id=%s dropped=%r", resume_id, cut
        )
    return [pinned for pinned in pins if _line_id(pinned) not in cut]


def _plan_record(plan: Plan, repo_groups: list[dict[str, Any]]) -> dict[str, Any]:
    """计划进信封的形态:每条线给了多少预算、实际用了多少。

    实际用量取自各线自己的探索元信息;被砍掉、跑失败、没进探索段的线没有用量。
    给了多少与用了多少放在一起,是为了日后按真实用量重定各档的数值。
    """
    explored = {
        str(group.get("repo", "")): (group.get("qbank_v2") or {}).get("explore_meta") or {}
        for group in repo_groups
    }
    return {
        "status": plan.status,
        "prompt_version": plan.prompt_version,
        "usage": plan.usage,
        "lines": [
            {
                "repo": line.line_id,
                "tier": line.tier,
                "focus": list(line.focus),
                "max_turns": line.budget.max_turns,
                "max_wall_seconds": line.budget.max_wall_seconds,
                "turns_used": explored.get(line.line_id, {}).get("turns"),
                "seconds_used": explored.get(line.line_id, {}).get("seconds"),
            }
            for line in plan.lines
        ],
    }


def _keep_first_cv_dive(
    repo_groups: list[dict[str, Any]], *, resume_id: int
) -> list[dict[str, Any]]:
    """简历深挖全局只留一条:按分线顺序保留第一组,其余丢弃并留痕。

    简历自述只有一份,几条线各自回退到简历深挖时拿到的是同一份材料,题目也就
    近乎相同。分线(_repo_lines)已把读不到的仓合成一条线,这里裁的是分线时
    可读、进了子图却恰好读不到(限流等瞬时故障)的仓。
    """
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    has_cv_dive = False
    for group in repo_groups:
        if group.get("group") != "cv_dive":
            kept.append(group)
        elif has_cv_dive:
            dropped.append(str(group.get("repo", "")))
        else:
            has_cv_dive = True
            kept.append(group)
    if dropped:
        logging.getLogger(__name__).warning(
            "bundle_cv_dive_dedup resume_id=%s dropped=%r", resume_id, dropped
        )
    return kept


async def _contained[T](name: str, line: Awaitable[T], *, resume_id: int, fallback: T) -> T:
    """把一条证据线的异常关在线内:降级为 fallback 并留痕。

    各线自己处理了预期内的失败(取数被拒、搜索不可达);这里兜的是没料到的那
    一类。并发执行时放它出去,除了整包失败,其余还在跑的线会成为没人收结果的
    孤儿任务,白花模型调用。
    """
    try:
        return await line
    except Exception:  # noqa: BLE001 — 单线失败只损失该线
        logging.getLogger(__name__).warning(
            "证据线失败 line=%s resume=%s", name, resume_id, exc_info=True
        )
        return fallback


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
        failure_listing = f"评测失败清单(任务/test):\n{material}"
        ag_questions = await _b4_questions(
            failure_listing,
            count_min=2,
            count_max=3,
            source="autograding-failures",
        )
        group: dict[str, Any] = {
            "group": "autograding",
            "mode": "error_analysis",
            "repo_summary": f"评测非满分,失败 {len(failures)} 项",
            "questions": ag_questions,
            "prompt_version": _prompt_version(),
        }
        if review_mode() == "annotate":
            # 审题看的是出题时的同一份材料;意见挂在组上,题目不动
            group["review"] = await review_line(
                targets_from_questions(ag_questions), material=failure_listing
            )
        return group
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


def _aggregate_usage(
    groups: list[dict[str, Any]], *, plan_usage: dict[str, int | None] | None = None
) -> dict[str, int | None]:
    """聚合模型用量:规划调用,仓线的探索、出题、审题,以及评测线的审题。

    评测线、兜底线的出题调用不采用量,所以不在这里。
    """
    usage_total: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
    }
    metas: list[dict[str, Any]] = [plan_usage or {}]
    for g in groups:
        qbank_v2 = g.get("qbank_v2") or {}
        metas += [
            qbank_v2.get("explore_meta") or {},
            qbank_v2.get("generation_usage") or {},
            (qbank_v2.get("review") or {}).get("usage") or {},
            (qbank_v2.get("review") or {}).get("revise_usage") or {},
            (g.get("review") or {}).get("usage") or {},
        ]
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
      而简历深挖全局只留一条线
    - 评测线:有评测记录且非满分 → 失败 test 错因追问
    - 奖项线:简历有奖项 → 背景卡+纯过程追问;搜索通道不可用 → 不可考
    - 兜底线:以上全无 → 基础三维 + 部门技能题组

    仓线、评测线、奖项线并发执行;单线失败只损失该线(仓线留错误组,评测线与
    奖项线缺席)。信封里组的顺序固定为仓线(简历里的出现序)→ 评测 → 奖项 →
    兜底,与各线完成的先后无关。

    起线之前先给可读的仓定探索计划(evaluation/plan.py):每条线的档位与预算、
    整包封顶。计划连同各线实际用掉的轮数与时间记在信封的 plan 里。

    grade 是**元信息**(年级原文):只透传给调查子图调节出题深度,不参与
    任何评分线;缺省空串即「年级缺失」,子图走安全默认。
    """
    provider = search_provider or NullSearchProvider()
    project_text = _project_text(fields)
    resume_text = _resume_text(fields)

    # 多仓:项目文本里每个 GitHub 仓各深挖一次,产出独立 repo group。
    pins, repo_metas = await _repo_lines(
        project_text, resume_id=resume_id, github_token=github_token
    )
    # 只有可读的仓会进探索段,所以只给它们定预算;make_plan 不抛,规划不成即缺省
    plan = await make_plan(
        [
            RepoCandidate.from_repo_meta(owner, name, meta, candidate_login=github_key or "")
            for (owner, name), meta in repo_metas.items()
        ],
        project_text=project_text,
    )
    repo_groups, autograding_group, awards_group = await asyncio.gather(
        _repo_groups(
            _without_cut_lines(pins, plan, resume_id=resume_id),
            project_text=project_text,
            resume_text=resume_text,
            github_key=github_key,
            github_token=github_token,
            grade=grade,
            plan=plan,
        ),
        _contained(
            "autograding",
            _autograding_line(github_key, resume_id),
            resume_id=resume_id,
            fallback=None,
        ),
        _contained(
            "awards",
            _awards_line(provider, fields, resume_id),
            resume_id=resume_id,
            fallback=None,
        ),
    )

    # 用量按实际跑过的线算,所以取在裁重复组之前:被裁掉的线同样花了 token。
    other_groups = [g for g in (autograding_group, awards_group) if g is not None]
    explore_usage_total = _aggregate_usage([*repo_groups, *other_groups], plan_usage=plan.usage)
    groups = _keep_first_cv_dive(repo_groups, resume_id=resume_id)
    groups.extend(other_groups)
    if not any(_group_has_evidence(g) for g in groups):
        groups.append(await _fallback_line(fields))

    all_questions = _flatten_v2_questions(groups)
    return {
        "schema_name": "evaluation_qbank/v2",
        "groups": groups,
        "plan": _plan_record(plan, repo_groups),
        "suggested_plan": suggest_plan(all_questions, budget_minutes=15),
        "total_questions": len(all_questions),
        "explore_usage_total": explore_usage_total,
        "prompt_version": _prompt_version(),
    }
