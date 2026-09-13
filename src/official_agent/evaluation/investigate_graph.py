"""调查子图·仓深挖(B3,#130):路由 → 取仓 → 值得度 → 四证据锚题。

- 与 B1 评分子图平行(一总图两子图的调查子图,#126)
- 生成轨与 B1 相同:提示词 JSON + strict Pydantic;后置校验(评审 P1 同款):
  deep_dive 题必带 evidence.path 且路径必须真实存在于仓;guided 题不得带仓路径
- GitHub 不可达不是失败:降级 guided(注明 unavailable),不阻塞任务
"""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from official_agent.config import get_effective_settings
from official_agent.evaluation.attribution import (
    RepoAttribution,
    attribute,
    detect_contribution_target,
    resolve_entry,
)
from official_agent.evaluation.explore import run_explore
from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable
from official_agent.evaluation.graph import _extract_json
from official_agent.evaluation.investigate import extract_repo, route_project
from official_agent.evaluation.schema import (
    ExploreMeta,
    QbankV2,
    QuestionGroupV2,
    UsageMeta,
)
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt, load_prompt_meta

PROMPT_FILE = "evaluation/grilling.md"
SCORING_TEMPERATURE = 0.2


def _prompt_version() -> str:
    return load_prompt_meta(PROMPT_FILE).get("version", "unknown")


class InvestigationState(TypedDict, total=False):
    project_text: str
    github_base: str
    github_token: str
    candidate_login: str
    route: str
    repo_owner: str
    repo_name: str
    default_branch: str
    dossier_text: str
    dossier_degraded: bool
    dossier_degrade_reason: str
    dossier_turns: int
    explore_usage: dict
    attribution: dict
    paths: list[str]
    paths_truncated: bool
    question_set: dict[str, Any]
    error: str | None


async def route_node(state: InvestigationState) -> dict:
    """提取仓位置并探测可读性 → 路由(#130);入口瀑布+归属四级(#150)。

    支持 M-1 多仓:调用方可预置 repo_owner/repo_name 钉住某仓(逐仓深挖);
    未钉时走瀑布:简历 URL 直配 → 绑定登录名匹配 → GitHub 搜索兜底。
    unverified(搜索撞名)不深挖只 guided(ADR-0008)。
    """
    text = state["project_text"]
    login = state.get("candidate_login", "")
    pinned_owner = state.get("repo_owner")
    pinned_name = state.get("repo_name")
    repo = (pinned_owner, pinned_name) if pinned_owner and pinned_name else extract_repo(text)
    if repo is None and not login and detect_contribution_target(text) is None:
        # 无仓位置且无登录名/贡献声明:不建 client(零 GitHub 调用,skip 断言依赖此)
        return {"route": route_project(text, None)}
    client = GitHubClient(
        base_url=state.get("github_base") or "https://api.github.com",
        token=state.get("github_token") or "",
    )
    found: RepoAttribution | None = None
    if repo is None:
        # 贡献声明优先于绑定匹配/搜索(D4):「仓+贡献动词」是明确点名,
        # 绑定并查到 commits/PR → trusted-contribution 深挖;未绑定/无证据
        # → claimed,不深挖只出过程题
        target = detect_contribution_target(text)
        if target:
            found = await attribute(*target, login=login, source="contribution", client=client)
            if found.deep_dive_allowed:
                repo = target
            else:
                return {
                    "route": "guided",
                    "repo_owner": target[0],
                    "repo_name": target[1],
                    "attribution": {
                        "owner": found.owner,
                        "name": found.name,
                        "level": found.level,
                        "evidence": found.evidence,
                        "source": found.source,
                    },
                }
    if repo is None and login:
        # 瀑布第 2/3 步:绑定名下匹配 / 搜索兜底(第 1 步已被 extract_repo 覆盖)
        try:
            found = await resolve_entry(text, login=login, client=client)
        except GitHubUnavailable:
            found = None
        if found:
            repo = (found.owner, found.name)
    if repo is None:
        return {"route": route_project(text, None)}
    try:
        meta = await client.repo(*repo)
        branch = meta.get("default_branch") or "main"
    except GitHubUnavailable:
        degraded: dict = {
            "route": route_project(text, False),
            "repo_owner": repo[0],
            "repo_name": repo[1],
        }
        if found is not None:
            degraded["attribution"] = {
                "owner": found.owner,
                "name": found.name,
                "level": found.level,
                "evidence": found.evidence,
                "source": found.source,
            }
        return degraded
    if found is None:
        # 钉住/URL 直配的仓:简历自述来源 → source=url(归属内部自查 commits/PR)
        found = await attribute(repo[0], repo[1], login=login, source="url", client=client)
    readable = True
    route = route_project(text, readable)
    if not found.deep_dive_allowed:
        # unverified:仓存在也不深挖,仅 guided(ADR-0008)
        route = "guided"
    return {
        "route": route,
        "repo_owner": repo[0],
        "repo_name": repo[1],
        "default_branch": str(branch),
        "attribution": {
            "owner": found.owner,
            "name": found.name,
            "level": found.level,
            "evidence": found.evidence,
            "source": found.source,
        },
    }


def route_after_route(state: InvestigationState) -> str:
    return state["route"]  # deep_dive | guided | skip


async def explore_node(state: InvestigationState) -> dict:
    """探索段(B-AG3 #151):受限 ReAct 循环产出 dossier,替换 fetch_node 固定取材。

    - 预算四闸在 explore_repo 内(轮数/墙钟/client 截断/dossier 40K);触顶标
      degraded,用已有材料出题(D7,不判失败)。
    - dossier 为空 = GitHub 不可达/探索全败 → 降级 guided(spec §3.4)。
    - worthiness 信号仅用于题数分档(D10:退役是 #152 schema v2 的事)。
    """
    attribution_dict = state.get("attribution") or {}
    attribution = str(attribution_dict.get("level", ""))
    dossier = await run_explore(
        state["project_text"],
        owner=state["repo_owner"],
        name=state["repo_name"],
        attribution=attribution,
        login=state.get("candidate_login", ""),
        github_base=state.get("github_base") or "https://api.github.com",
        github_token=state.get("github_token") or "",
    )
    if dossier.is_empty():
        return {
            "route": "guided",
            "error": None,
            "dossier_text": (
                f"探索段未取得材料({dossier.degrade_reason or '无观察'}),降级通用引导题"
            ),
            "dossier_degraded": True,
            "dossier_degrade_reason": dossier.degrade_reason,
            "dossier_turns": dossier.turns_used,
        }
    if dossier.degraded:
        # 预算触顶:用已有材料出题(D7),降级标记进材料头,题面可感知
        degrade_note = f"[探索降级:{dossier.degrade_reason}]"
        dossier_text = degrade_note + "\n\n" + dossier.render()
    else:
        dossier_text = dossier.render()
    return {
        "dossier_text": dossier_text,
        "paths": dossier.paths,
        "paths_truncated": dossier.paths_truncated,
        "dossier_degraded": dossier.degraded,
        "dossier_degrade_reason": dossier.degrade_reason,
        "dossier_turns": dossier.turns_used,
        "explore_usage": {
            "input_tokens": dossier.input_tokens,
            "output_tokens": dossier.output_tokens,
            "cache_hit_tokens": dossier.cache_hit_tokens,
            "cache_miss_tokens": dossier.cache_miss_tokens,
        },
        "error": None,
    }


async def generate_node(state: InvestigationState) -> dict:
    """出题段(B-AG4 #152):dossier → 题组 v2(单次结构化调用,spec §3.3)。

    - 模型只出题组 JSON;探索元信息由代码注入(explore state),不进模型面。
    - 后置校验:题量硬顶 15 / 敷衍 dossier ≤3 / 路径白名单(dossier 出现过的
      路径)/ 对抗前提黑名单 / 链 theme 源自 dossier。违规进 error 态,B2 重试。
    - guided:1 道通用引导题(entry 形状,chains 空)。
    """
    try:
        deep = state["route"] == "deep_dive"
        dossier_text = state.get("dossier_text", "")
        thin = len(dossier_text.strip()) < 400  # 敷衍 dossier(D10:1-2 题合法)
        settings = get_effective_settings()
        model = build_model(settings, temperature=SCORING_TEMPERATURE)
        # ADR-0004:代码零 prompt 字符串——体量标记是数据,规则文本全在
        # prompts/evaluation/grilling.md(敷衍/充足两套规则文件内已有)
        material_note = f"材料体量={'贫乏' if thin else '充足'}。"
        prompt_text = (
            load_prompt(PROMPT_FILE)
            + "\n\n---\n\n候选人自述:\n"
            + state["project_text"]
            + "\n\ndossier 材料:\n"
            + dossier_text
            + "\n\n"
            + material_note
        )
        resp = await model.ainvoke([HumanMessage(content=prompt_text)])
        # 出题段单次 usage(#154/D9):raw token_usage 优先(DeepSeek cache 字段)
        from official_agent.state.conversation import extract_usage

        gen_usage = extract_usage(
            (getattr(resp, "response_metadata", None) or {}).get("token_usage")
            or getattr(resp, "usage_metadata", None)
        )
        raw = resp.content
        if isinstance(raw, list):
            raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
        content = raw if isinstance(raw, str) else str(raw)
        group_payload: dict[str, Any] = json.loads(_extract_json(content))
        repo_summary = str(group_payload.get("repo_summary", ""))

        deep = bool(deep)
        if deep:
            group = validate_qbank_v2_group(
                group_payload,
                dossier_text,
                paths=list(state.get("paths", [])),
                thin=thin,
            )
        else:
            # guided:entry 引导题,chains 空;黑名单与「不带仓路径」不变量仍适用
            guided_view = {
                "entry": group_payload.get("entry"),
                "chains": [],
                "reserves": group_payload.get("reserves", []),
            }
            _validate_group_v2(guided_view, dossier_text, [])
            guided_payload: dict[str, Any] = {
                "entry": group_payload.get("entry")
                or {
                    "category": "C1_背景与动机",
                    "question": "请讲讲这个项目:你负责哪部分?最大的收获是什么?",
                    "answer_reference": {
                        "strong": "讲清职责与收获",
                        "acceptable": "讲清职责",
                        "weak": "含糊其辞",
                    },
                    "evidence": {"path": "", "note": "仓不可读,通用引导"},
                    "time_minutes": 3,
                },
                "chains": [],
                "reserves": [],
            }
            group = QuestionGroupV2.model_validate(guided_payload)

        envelope = QbankV2(
            repo_summary=repo_summary,
            group=group,
            mode="repo_deep_dive" if deep else "guided",
            attribution=_attribution_level((state.get("attribution") or {}).get("level")),
            degraded=bool(state.get("dossier_degraded")),
            degrade_reason=str(state.get("dossier_degrade_reason", "")),
            explore_meta=ExploreMeta(
                turns=int(state.get("dossier_turns", 0)),
                dossier_chars=len(dossier_text),
                **(state.get("explore_usage") or {}),
            ),
            generation_usage=(
                UsageMeta(**{k: v for k, v in gen_usage.items() if v is not None})
                if any(v is not None for v in gen_usage.values())
                else None
            ),
            prompt_version=_prompt_version(),
        )
        return {"question_set": envelope.model_dump(), "error": None}
    except Exception as exc:  # noqa: BLE001 — 失败进 error 态,B2 可重试
        return {"question_set": None, "error": f"{type(exc).__name__}: {exc}"}


#: 对抗前提黑名单(spec §3.3:「你自述了X…但仓库却是Y…请解释矛盾」式)
#: 注意:「自述」单独出现是合法锚定(简历锚定横切),不在黑名单
_ADVERSARIAL_WORDS = ("矛盾", "撒谎", "撒了谎", "夸大", "打脸", "为什么没做到")


def _validate_group_v2(payload: dict[str, Any], dossier_text: str, paths: list[str]) -> None:
    """v2 后置校验(#152):对抗前提黑名单/路径白名单/链源真实性。

    局限(诚实边界):链源真实性只对拉丁词元可判定,纯中文 theme 跳过
    (由 grilling prompt 铁律约束);dossier 文本为扫描全集。"""
    dossier_lowers = dossier_text.lower()

    def _check_question(q: str) -> None:
        for word in _ADVERSARIAL_WORDS:
            if word in q:
                raise ValueError(f"对抗前提问法({word}):{q[:40]!r}")

    def _check_path(p: str, where: str) -> None:
        if p and paths_set and p not in paths_set:
            raise ValueError(f"evidence.path 不在仓内({where}):{p!r}")

    paths_set = {x.strip() for x in paths if x and x.strip()}
    for chain in payload.get("chains", []):
        theme = str(chain.get("theme", ""))
        texts = [theme]
        for layer in chain.get("layers", []):
            q = str(layer.get("question", ""))
            _check_question(q)
            texts.append(q)
        # 链源真实性:链文本的拉丁词元至少一个出现在 dossier
        tokens = [
            t.lower()
            for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", " ".join(texts))
            if t.lower() not in {"the", "and", "for", "with", "layer"}
        ]
        if tokens and not any(t in dossier_lowers for t in tokens):
            raise ValueError(f"链源不在 dossier:theme={theme[:40]!r}")
    for reserve in payload.get("reserves", []):
        _check_question(str(reserve.get("question", "")))
        _check_path(
            str((reserve.get("evidence") or {}).get("path", "")),
            f"reserve:{reserve.get('category', '?')}",
        )
    entry = payload.get("entry") or {}
    _check_question(str(entry.get("question", "")))
    _check_path(str((entry.get("evidence") or {}).get("path", "")), "entry")


def validate_qbank_v2_group(
    group_payload: dict[str, Any],
    dossier_text: str,
    *,
    paths: list[str],
    thin: bool = False,
) -> QuestionGroupV2:
    """题组 v2 全量校验(#155 探针与 generate 共用;六探针的判定机器)。

    - 对抗前提黑名单/路径白名单/链源真实性(_validate_group_v2);
    - 结构(D12):入口 1 + 链 2-4×3-5 层 + 总量硬顶 15 + 敷衍 dossier ≤3。
    返回校验过的 QuestionGroupV2;违例 ValueError(→ generate error 态重试)。
    """
    _validate_group_v2(group_payload, dossier_text, paths)
    group = QuestionGroupV2.model_validate(
        {k: group_payload[k] for k in ("entry", "chains", "reserves") if k in group_payload}
    )
    if thin and group.total_questions > 3:
        raise ValueError(f"敷衍 dossier 题量越界:{group.total_questions} > 3")
    if group.total_questions > 15:
        raise ValueError(f"题量超硬顶:{group.total_questions} > 15")
    if group.entry is None:
        raise ValueError("deep_dive 缺入口题(D12:入口 1)")
    if not group.entry.evidence.path and not group.entry.evidence.note:
        raise ValueError("deep_dive 题空路径须有 evidence.note")
    if len(group.chains) < 2:
        raise ValueError(f"追问链不足:要求 2-4,模型给 {len(group.chains)}")
    for chain in group.chains:
        if not 3 <= len(chain.layers) <= 5:
            raise ValueError(f"链层数越界({chain.theme[:16]!r}):{len(chain.layers)}")
    return group


def _attribution_level(level: Any) -> Any:
    """state 归属级别 → schema Literal(未知名回退 none,防模型/上游噪音)。"""
    allowed = {"trusted-own", "trusted-contribution", "claimed", "unverified", "none"}
    return level if level in allowed else "none"


async def skip_node(state: InvestigationState) -> dict:
    """skip:此维不出题(空集合法,#130);信封 v2 形状。"""
    envelope = QbankV2(mode="skipped", group=QuestionGroupV2(), prompt_version=_prompt_version())
    return {"question_set": envelope.model_dump(), "error": None}


async def finalize(state: InvestigationState) -> dict:
    return {}


_compiled: Any | None = None


def build_investigation_subgraph() -> Any:
    """调查子图:route →(deep_dive→fetch)/guided/skip → generate → finalize。"""
    global _compiled
    if _compiled is not None:
        return _compiled
    g = StateGraph(InvestigationState)
    g.add_node("route", route_node)
    g.add_node("explore", explore_node)
    g.add_node("generate", generate_node)
    g.add_node("skip", skip_node)
    g.add_node("finalize", finalize)
    g.set_entry_point("route")
    g.add_conditional_edges(
        "route",
        route_after_route,
        {
            "deep_dive": "explore",
            "guided": "generate",
            "skip": "skip",
        },
    )
    g.add_edge("explore", "generate")
    g.add_edge("generate", "finalize")
    g.add_edge("skip", "finalize")
    g.add_edge("finalize", END)
    _compiled = g.compile()
    return _compiled


async def run_investigation(
    project_text: str,
    *,
    repo: tuple[str, str] | None = None,
    github_base: str = "https://api.github.com",
    github_token: str = "",
    candidate_login: str = "",
) -> dict:
    """便捷入口:返回题集 dict(questions 可为空=skip/降级);LLM 失败抛 RuntimeError。

    M-1:可钉 repo(owner, repo) 逐仓调查(多仓候选一个仓一个 envelope);
    缺省按项目文本首个 GitHub URL。
    """
    graph = build_investigation_subgraph()
    init: InvestigationState = {
        "project_text": project_text,
        "github_base": github_base,
        "github_token": github_token,
        "candidate_login": candidate_login,
    }
    if repo:
        init["repo_owner"], init["repo_name"] = repo
    final = await graph.ainvoke(init)
    if final.get("error") or final.get("question_set") is None:
        raise RuntimeError(f"调查子图失败:{final.get('error')}")
    return final["question_set"]
