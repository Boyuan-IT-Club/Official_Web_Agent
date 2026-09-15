"""调查子图·仓深挖:路由 → 取仓 → 值得度 → 四证据锚题。

- 与评分子图平行(一总图两子图的调查子图)
- 生成轨与评分轨相同:提示词 JSON + strict Pydantic;后置校验:
  deep_dive 题必带 evidence.path 且路径必须真实存在于仓;guided 题不得带仓路径
- GitHub 不可达不是失败:降级 guided(注明 unavailable),不阻塞任务
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
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
from official_agent.evaluation.dossier import dossier_from_resume
from official_agent.evaluation.explore import run_explore
from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable
from official_agent.evaluation.graph import _extract_json
from official_agent.evaluation.investigate import (
    DEFAULT_GRADE_BAND,
    GRADE_BAND_LABELS,
    LAYER_BOUNDS,
    GradeBand,
    extract_repo,
    grade_band,
    route_project,
)
from official_agent.evaluation.schema import (
    MAX_CHAINS,
    ExploreMeta,
    QbankV2,
    QuestionGroupV2,
    UsageMeta,
)
from official_agent.evaluation.tech_stack import _normalize, extract_tech_stack
from official_agent.graphs.assistant import build_model
from official_agent.prompt_loader import load_prompt, load_prompt_meta

#: 简历深挖的出题 prompt(与仓深挖的 grilling 分开:材料性质、出题主线都不同)
CV_PROMPT_FILE = "evaluation/cv_dive.md"
#: 技术栈清单在材料里的数据表头(规则文本在 cv_dive.md 里,ADR-0004)
TECH_STACK_HEADER = "技术栈清单:\n"

PROMPT_FILE = "evaluation/grilling.md"
SCORING_TEMPERATURE = 0.2


def _prompt_version(prompt_file: str = PROMPT_FILE) -> str:
    """信封落盘的 prompt 版本锚(ADR-0004)。

    必须与**本次实际使用**的 prompt 一致:两条路径各有一套 prompt,写错版本
    会让改 prompt 后的评测归因与重跑比对照错对象。
    """
    return load_prompt_meta(prompt_file).get("version", "unknown")


class InvestigationState(TypedDict, total=False):
    project_text: str
    #: 简历全文(不含项目栏)——技术栈常单列一栏,只喂项目文本会让抽取器
    #: 看不见它。缺省退化为 project_text(单栏调用方的兼容面)。
    resume_text: str
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
    tech_items: list[dict]
    attribution: dict
    #: 出题深度档,由年级派生;元信息,不进评分
    grade_band: GradeBand
    paths: list[str]
    paths_truncated: bool
    question_set: dict[str, Any]
    error: str | None


def _render_tech(state: InvestigationState) -> str:
    """技术栈清单 → 出题材料的文本块(名词 + 自述档位 + 项目归属)。

    档位进材料是让模型按声称强度定深度(熟练才深挖);项目归属进材料是让
    模型把技术追问落到具体项目上(「这个技术你在 X 里怎么用的」)。
    """
    items = state.get("tech_items") or []
    if not items:
        return "(未抽出技术栈;只依据上面的项目经验出题)"
    lines = []
    for it in items:
        used = "、".join(it.get("used_in") or []) or "简历未指明项目"
        lines.append(f"- {it.get('name')}(自述档位:{it.get('claimed_level')};出现在:{used})")
    return "\n".join(lines)

async def _norepo_route(state: InvestigationState) -> dict:
    """无仓位置时的路由:抽技术栈,连同项目栏内容一起定路径。

    无仓时技术栈抽两次都没有意义,且**出题段本来就依赖它**(每名词一条
    技术链),所以这里只抽一次、结果全程复用,不再按「项目栏有没有料」省调用。
    """
    text = state["project_text"]
    items = await _extract_tech(state)
    return {"route": route_project(text, None, tech_count=len(items)), "tech_items": items}


async def _extract_tech(state: InvestigationState) -> list[dict]:
    """抽技术栈(简历全文优先);抽取器故障不挡路由,按「无技术栈」继续。"""
    try:
        items = await extract_tech_stack(state.get("resume_text") or state["project_text"])
    except Exception:  # noqa: BLE001 — 基础设施故障不该把简历判成没料
        logging.getLogger(__name__).warning(
            "guard_event guard_name=tech_stack_extract verdict=failed", exc_info=True
        )
        return []
    return [asdict(item) for item in items]


async def route_node(state: InvestigationState) -> dict:
    """提取仓位置并探测可读性 → 路由;入口瀑布+归属四级。

    支持多仓:调用方可预置 repo_owner/repo_name 钉住某仓(逐仓深挖);
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
        return await _norepo_route(state)
    client = GitHubClient(
        base_url=state.get("github_base") or "https://api.github.com",
        token=state.get("github_token") or "",
    )
    found: RepoAttribution | None = None
    if repo is None:
        # 贡献声明优先于绑定匹配/搜索:「仓+贡献动词」是明确点名,
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
        return await _norepo_route(state)
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
    return state["route"]  # deep_dive | cv_dive | guided | skip



def _resume_dossier(state: InvestigationState) -> dict:
    """无仓路径的取材:简历文本 → 档案。

    档案给模型当出题材料。简历文本为空理论上已被路由挡掉(空栏走 skip),
    此处不再降级——路由已经决定了走 cv_dive。
    """
    text = state.get("resume_text") or state["project_text"]
    dossier = dossier_from_resume(text, attribution="cv")
    return {
        "dossier_text": dossier.render(),
        "paths": [],
        "paths_truncated": False,
        "dossier_degraded": False,
        "dossier_degrade_reason": "",
        "dossier_turns": 0,
        "explore_usage": {},
        "error": None,
    }

async def explore_node(state: InvestigationState) -> dict:
    """探索段:有仓走 ReAct 取材,无仓用简历文本当材料。

    - 有仓(deep_dive):受限 ReAct 循环产出 dossier;预算四闸在 explore_repo
      内(轮数/墙钟/client 截断/dossier 40K);触顶标 degraded,用已有材料
      出题(不判失败)。
    - 无仓(cv_dive):简历文本没有仓可探,直接按板块取材成档案——不再产出
      「探索段未取得材料」的空档案,出题段也就有料可依。
    - 有仓但探索全败 → 降级 guided(既有语义不变)。
    """
    if state.get("route") == "cv_dive":
        return _resume_dossier(state)
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
        # 预算触顶:用已有材料出题,降级标记进材料头,题面可感知
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
    """出题段:dossier → 题组 v2(单次结构化调用)。

    - 模型只出题组 JSON;探索元信息由代码注入(explore state),不进模型面。
    - 后置校验:题量硬顶 15 / 敷衍 dossier ≤3 / 路径白名单(dossier 出现过的
      路径)/ 对抗前提黑名单 / 链 theme 源自 dossier。违规进 error 态,调用方重试。
    - guided:1 道通用引导题(entry 形状,chains 空)。
    """
    try:
        deep = state["route"] == "deep_dive"
        cv = state["route"] == "cv_dive"
        dossier_text = state.get("dossier_text", "")
        # 简历路径不套用体量阈值:短简历是正常简历,不是敷衍档案(误杀短但
        # 具体的简历正是要避免的);仓路径的体量标记维持原样。
        thin = (not cv) and len(dossier_text.strip()) < 400
        settings = get_effective_settings()
        model = build_model(settings, temperature=SCORING_TEMPERATURE)
        # ADR-0004:prompt 放文件,代码只拼**数据**(材料与标记);两条路径的
        # 规则文本各在自己的文件里,故按路径选文件。
        prompt_file = CV_PROMPT_FILE if cv else PROMPT_FILE
        material_note = f"材料体量={'贫乏' if thin else '充足'}。"
        # 简历路径在档案之外**追加**技术栈清单(供逐技术出题);档案本身必须
        # 保留——简历的项目栏、自我介绍、实习经历都在里面,项目深挖链靠它取材。
        material = "dossier 材料:\n" + dossier_text
        if cv:
            material += "\n\n" + TECH_STACK_HEADER + _render_tech(state)
            # 年级**原文不进材料**(那一栏并不干净,后端记录过里面存着姓名);
            # 只给派生的档位标签,让 prompt 据此定链长与深度。
            band = state.get("grade_band") or DEFAULT_GRADE_BAND
            material += f"\n\n出题深度档:{GRADE_BAND_LABELS.get(band, band)}。"
        prompt_text = (
            load_prompt(prompt_file)
            + "\n\n---\n\n候选人自述:\n"
            + state["project_text"]
            + "\n\n"
            + material
            + "\n\n"
            + material_note
        )

        # 结构化输出的形状由 strict schema 兜住,但**语义**后置校验(链层数、
        # 三档答案齐备、主题出自材料、题量硬顶)会偶发不过。这些错误对模型是
        # 可自纠的,直接进 error 态会让整份简历一道题都拿不到 —— 故照评分轨
        # 的先例,把校验错误回灌、子图内重试一次,两次仍不合规才翻 error。
        from official_agent.state.conversation import extract_usage

        gen_usage: dict[str, Any] = {}
        group = None
        corrective = ""
        last_err: Exception | None = None
        for _attempt in range(2):
            resp = await model.ainvoke([HumanMessage(content=prompt_text + corrective)])
            gen_usage = extract_usage(
                (getattr(resp, "response_metadata", None) or {}).get("token_usage")
                or getattr(resp, "usage_metadata", None)
            )
            raw = resp.content
            if isinstance(raw, list):
                raw = "".join(b.get("text", "") for b in raw if isinstance(b, dict))
            content = raw if isinstance(raw, str) else str(raw)
            try:
                group_payload: dict[str, Any] = json.loads(_extract_json(content))
                repo_summary = str(group_payload.get("repo_summary", ""))
                deep = bool(deep)
                if deep or cv:
                    # 两条路径共用同一台校验机器;区别只在 no_repo:简历没有仓,
                    # 题面若带仓内路径,那必然是模型的臆造。
                    # 年级档只作用于**简历路径**:仓路径的 prompt 没有档位段,
                    # 层数要求仍是 3-5。把简历档套到仓路径上会形成「prompt 要
                    # 3-5 层、校验只收 1-2 层」的死结,两次重试都不合规,整份
                    # 候选人一题都拿不到。
                    band = (
                        state.get("grade_band") or DEFAULT_GRADE_BAND
                    ) if cv else DEFAULT_GRADE_BAND
                    group = validate_qbank_v2_group(
                        group_payload,
                        dossier_text,
                        paths=list(state.get("paths", [])),
                        thin=thin,
                        no_repo=cv,
                        grade_band=band,
                    )
                else:
                    group = _guided_group(group_payload, dossier_text)
                break
            except Exception as exc:  # noqa: BLE001 — 回灌错误让模型自纠
                last_err = exc
                corrective = (
                    "\n\n---\n\n上次输出不合规,错误信息如下,请据此修正后"
                    f"**重新输出完整 JSON**(不要解释、不要只给差异):\n{exc}"
                )
        if group is None:
            raise ValueError(f"出题两次仍不合规:{last_err}")

        envelope = QbankV2(
            repo_summary=repo_summary,
            group=group,
            mode=("repo_deep_dive" if deep else "cv_dive" if cv else "guided"),
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
            prompt_version=_prompt_version(prompt_file),
        )
        return {"question_set": envelope.model_dump(), "error": None}
    except Exception as exc:  # noqa: BLE001 — 失败进 error 态,可由调用方重试
        return {"question_set": None, "error": f"{type(exc).__name__}: {exc}"}


#: 对抗前提黑名单(「你自述了X…但仓库却是Y…请解释矛盾」式)
#: 注意:「自述」单独出现是合法锚定(简历锚定横切),不在黑名单
_ADVERSARIAL_WORDS = ("矛盾", "撒谎", "撒了谎", "夸大", "打脸", "为什么没做到")

#: 链源真实性校验的忽略词:**只收我们自己材料里的拴架词**——模型会把材料
#: 标签原样抄进 theme(如「源自 dossier C4 项目经验栏」),那是我们的内部
#: 用词、不在候选人材料里,当内容词会把真实链误判成编造。
#: 英文停用词一并忽略(它们不承载来源信息)。
#: 收词必须克制:忽略词越多,「词元全被忽略」的编造链越容易蒙混过关。
_CHAIN_SOURCE_IGNORES = frozenset({"the", "and", "for", "with", "layer", "dossier"})


def _has_cjk(text: str) -> bool:
    """文本是否含中日韩文字(链源比对对中文不可判定,见校验器注释)。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _validate_group_v2(
    payload: dict[str, Any], dossier_text: str, paths: list[str], *, no_repo: bool = False
) -> None:
    """v2 后置校验:对抗前提黑名单/路径白名单/链源真实性。

    局限(诚实边界):链源真实性只对拉丁词元可判定,纯中文 theme 跳过
    (由出题 prompt 铁律约束);dossier 文本为扫描全集。"""
    dossier_lowers = dossier_text.lower()

    def _check_question(q: str) -> None:
        for word in _ADVERSARIAL_WORDS:
            if word in q:
                raise ValueError(f"对抗前提问法({word}):{q[:40]!r}")

    def _check_path(p: str, where: str) -> None:
        if not p:
            return
        if no_repo:
            # 简历路径没有仓:任何仓内路径都无从核实,必是模型臆造
            raise ValueError(f"无仓路径不得带仓内 evidence.path({where}):{p!r}")
        if paths_set and p not in paths_set:
            raise ValueError(f"evidence.path 不在仓内({where}):{p!r}")

    paths_set = {x.strip() for x in paths if x and x.strip()}
    for chain in payload.get("chains", []):
        theme = str(chain.get("theme", ""))
        texts = [theme]
        for layer in chain.get("layers", []):
            q = str(layer.get("question", ""))
            _check_question(q)
            texts.append(q)
        # 链源真实性(仓深挖):链文本的拉丁词元至少一个出现在 dossier。
        # 简历路径不走这条:那类 theme 是**技术名词**,像 `C++` 这种带符号的
        # 名字根本过不了词元正则(会被误判成编造);简历侧的等价保证由链主题
        # 与材料逐字比对承担(见 validate_qbank_v2_group 的 no_repo 分支)。
        if no_repo:
            continue
        joined = " ".join(texts)
        raw_tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", joined)
        tokens = [t.lower() for t in raw_tokens if t.lower() not in _CHAIN_SOURCE_IGNORES]
        theme_raw = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", theme)
        theme_tokens = [t.lower() for t in theme_raw if t.lower() not in _CHAIN_SOURCE_IGNORES]
        if theme_raw and not theme_tokens and not _has_cjk(theme):
            raise ValueError(f"链主题只有停用词,无可核对来源:theme={theme[:40]!r}")
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
    no_repo: bool = False,
    grade_band: GradeBand = DEFAULT_GRADE_BAND,
) -> QuestionGroupV2:
    """题组 v2 全量校验(探针与 generate 共用;六探针的判定机器)。

    - 对抗前提黑名单/路径白名单/链源真实性(_validate_group_v2);
    - 结构:入口 1 + 链 2-MAX_CHAINS + 总量硬顶 15 + 敷衍 dossier ≤3;
    - 链层数按**出题深度档**定界(grade_band):标准档 3-5 层,大一档 1-2 层
      ——深度策略只在这一处判定,schema 只留形状上界;
    - 简历路径(no_repo)额外两条:
      ① 题面引用的技术/项目名必须能在材料里找到(防编造)——链主题、入口题、
         备选题都要查:没开链的技术题正是落在 reserves 上,只查链会漏掉那一半;
      ② 链的每一层都要有三档参考答案(面试官不熟悉该技术,没有答案可依)。
    """
    _validate_group_v2(group_payload, dossier_text, paths, no_repo=no_repo)
    group = QuestionGroupV2.model_validate(
        {k: group_payload[k] for k in ("entry", "chains", "reserves") if k in group_payload}
    )
    if thin and group.total_questions > 3:
        raise ValueError(f"敷衍 dossier 题量越界:{group.total_questions} > 3")
    if group.total_questions > 15:
        raise ValueError(f"题量超硬顶:{group.total_questions} > 15")
    if group.entry is None:
        raise ValueError("题组缺入口题(入口必须 1 道)")
    if not group.entry.evidence.path and not group.entry.evidence.note:
        raise ValueError("题空路径须有 evidence.note")
    if len(group.chains) < 2:
        raise ValueError(f"追问链不足:要求 2-{MAX_CHAINS},模型给 {len(group.chains)}")
    material = _normalize(dossier_text)
    for chain in group.chains:
        low, high = LAYER_BOUNDS.get(grade_band, LAYER_BOUNDS[DEFAULT_GRADE_BAND])
        if not low <= len(chain.layers) <= high:
            raise ValueError(
                f"链层数越界({chain.theme[:16]!r}):{len(chain.layers)} 不在 {low}-{high}"
                f"({grade_band} 档)"
            )
        if no_repo:
            # 链的 theme 是这条链「问的是哪项技术/哪个项目」的声明,必须出自简历。
            if _normalize(chain.theme) not in material:
                raise ValueError(f"链主题不在简历材料里(疑似编造):{chain.theme[:30]!r}")
            missing = [
                i + 1 for i, layer in enumerate(chain.layers) if layer.answer_reference is None
            ]
            if missing:
                raise ValueError(f"链层缺三档参考答案({chain.theme[:16]!r})第 {missing} 层")
    return group


def _guided_group(group_payload: dict[str, Any], dossier_text: str) -> QuestionGroupV2:
    """guided:入口引导题,链与备选留空;黑名单与「不带仓路径」仍适用。"""
    guided_view = {
        "entry": group_payload.get("entry"),
        "chains": [],
        "reserves": group_payload.get("reserves", []),
    }
    _validate_group_v2(guided_view, dossier_text, [])
    return QuestionGroupV2.model_validate(
        {
            "entry": group_payload.get("entry")
            or {
                "category": "C1_背景与动机",
                "question": "请讲讲这个项目:你负责哪部分?最大的收获是什么?",
                "answer_reference": {
                    "strong": "讲清职责与收获",
                    "acceptable": "讲清职责",
                    "weak": "含糊其辞",
                },
                "evidence": {"path": "", "note": "材料不可读,通用引导"},
                "time_minutes": 3,
            },
            "chains": [],
            "reserves": [],
        }
    )


def _attribution_level(level: Any) -> Any:
    """state 归属级别 → schema Literal(未知名回退 none,防模型/上游噪音)。"""
    allowed = {"trusted-own", "trusted-contribution", "claimed", "unverified", "none"}
    return level if level in allowed else "none"


async def skip_node(state: InvestigationState) -> dict:
    """skip:此维不出题(空集合法);信封 v2 形状。"""
    envelope = QbankV2(mode="skipped", group=QuestionGroupV2(), prompt_version=_prompt_version())
    return {"question_set": envelope.model_dump(), "error": None}


async def finalize(state: InvestigationState) -> dict:
    return {}


_compiled: Any | None = None


def build_investigation_subgraph() -> Any:
    """调查子图:route →(deep_dive/cv_dive→explore)/guided/skip → generate → finalize。

    cv_dive 与 deep_dive 同走 explore→generate(都要取材后出带链的题组),
    区别在 explore 的取材来源(代码仓 vs 简历文本)与 generate 的出题 prompt。
    """
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
            "cv_dive": "explore",
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
    resume_text: str = "",
    repo: tuple[str, str] | None = None,
    github_base: str = "https://api.github.com",
    github_token: str = "",
    candidate_login: str = "",
    grade: str = "",
) -> dict:
    """便捷入口:返回题集 dict(questions 可为空=skip/降级);LLM 失败抛 RuntimeError。

    可钉 repo(owner, repo) 逐仓调查(多仓候选一个仓一个 envelope);
    缺省按项目文本首个 GitHub URL。
    resume_text 传简历全文(技术栈常单列一栏,只给项目文本会让无仓路径
    看不见它);缺省退化为 project_text,单栏调用方行为不变。
    grade 传年级原文(元信息):只用来派生**出题深度档**,不进评分;缺省即
    「年级缺失」,分档走标准档(与加档位之前的行为一致)。
    """
    graph = build_investigation_subgraph()
    init: InvestigationState = {
        "project_text": project_text,
        "resume_text": resume_text or project_text,
        "github_base": github_base,
        "github_token": github_token,
        "candidate_login": candidate_login,
        "grade_band": grade_band(grade),
    }
    if repo:
        init["repo_owner"], init["repo_name"] = repo
    final = await graph.ainvoke(init)
    if final.get("error") or final.get("question_set") is None:
        raise RuntimeError(f"调查子图失败:{final.get('error')}")
    return final["question_set"]
