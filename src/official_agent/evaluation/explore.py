"""探索段受限 ReAct 循环(B-AG3,#151;spec §3.2/D6/D7/D8)。

LLM(model_strong)决定调 D6 八工具之一 → 观察 → 观察**确定性映射**进
dossier 槽位(spec:agent 决定填多满,槽位归属不必 LLM 再判一次)。
预算四闸:①每仓 80 轮(LLM+工具累计)②墙钟 300s ③单工具结果截断
(client 层已做,#149)④dossier ≤40K(dossier.add 把关)。任一触顶即停,
用已有材料出题并标 degraded(D7:不判失败)。

prompt cache 纪律(D8):system(技能文本)跨候选字节稳定——不嵌时间戳/
仓主名/随机数;候选人材料只进 user 消息且追加不改写;步数计数放 user
消息尾部(每轮一条新 user 计数消息,历史只增)。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from official_agent.evaluation.dossier import SLOT_NAMES, Dossier
from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable
from official_agent.security.injection_guard import guard_tool_result
from official_agent.state.conversation import extract_usage

#: 预算闸(D7)
MAX_TURNS = 80
MAX_WALL_SECONDS = 300

#: 工具观察 → dossier 槽位(确定性映射;同观察逐槽全写,不做短路)
_S = {name: name for name in SLOT_NAMES}  # 槽名单源(与 schema.CATEGORY 同步)

_SLOT_BY_TOOL: dict[str, tuple[str, ...]] = {
    "repo_meta": (_S["C1_背景与动机"], _S["C2_技术选型与权衡"], _S["C5_数字与规模"]),
    "list_files": (_S["C3_架构与数据流"],),
    "read_file": (_S["C4_实现细节拷打"], _S["C7_边界与失败模式"]),
    "search_in_repo": (_S["C7_边界与失败模式"], _S["C10_复盘与改进"]),
    "read_commits": (_S["C6_难点与调试"],),
    "commit_detail": (_S["C6_难点与调试"], _S["C4_实现细节拷打"]),
    "search_issues": (_S["C8_真实性与贡献边界"],),
    "search_repos": (_S["C1_背景与动机"],),
    "list_user_repos": (_S["C1_背景与动机"],),
}
# C9 变更应力不单列映射:配置/CI/扩展点类 read_file 观察落 C4/C7 后由出题段
# 引用(spec「不必每类必有材料」);单列会令每次读码都灌 C9,稀释槽位语义。


def _observation_text(tool_name: str, payload: Any) -> str:
    """工具结果 → 观察文本(json 投影;client 层已截断,这里只转写)。"""
    import json

    if payload is None:
        return ""
    if tool_name == "list_files" and isinstance(payload, tuple):
        paths, truncated = payload
        body = ", ".join(paths)
        return f"路径清单({len(paths)} 条{' ,树截断' if truncated else ''}): {body}"
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


def build_explore_tools(client: GitHubClient, owner: str, name: str) -> list:
    """D6 八工具 → LangChain StructuredTool,**仓域预绑定**。

    owner/repo 由循环锁定(探索段不换仓,D2);模型侧签名只剩真正的参数
    (query/path/sha/author…),schema 来自显式签名+docstring。"""

    from langchain_core.tools import StructuredTool

    async def search_repos(query: str, per_page: int = 10) -> Any:
        """按项目名/关键词在 GitHub 全站搜索仓库(简历无链接且绑定无匹配时定位项目)。"""
        return await client.search_repos(query, per_page=per_page)

    async def list_user_repos(username: str, per_page: int = 100) -> Any:
        """列出某 GitHub 用户名下(own)仓库清单,核对绑定账号名下有哪些仓。"""
        return await client.list_user_repos(username, per_page=per_page)

    async def repo_meta() -> Any:
        """读锁定仓库的元数据:描述/语言/fork 与父仓/默认分支/stars/最近推送。"""
        return await client.repo_meta(owner, name)

    async def list_files(branch: str | None = None) -> Any:
        """列出锁定仓库文件路径清单(上限 600),了解结构与模块划分。"""
        return await client.list_files(owner, name, branch=branch)

    async def read_file(path: str, branch: str | None = None) -> Any:
        """读仓内单个文件(超 8K 字符截断留痕):README/核心源码/依赖清单/TODO。"""
        return await client.read_file(owner, name, path, branch=branch)

    async def search_in_repo(query: str, per_page: int = 20) -> Any:
        """仓内按关键词搜代码位置(返回路径清单),定位异常处理/TODO/关键实现。"""
        return await client.search_in_repo(owner, name, query, per_page=per_page)

    async def read_commits(author: str | None = None, per_page: int = 30) -> Any:
        """读提交历史(可按 author 过滤,含 sha):核对提交轨迹与开发叙事。"""
        return await client.read_commits(owner, name, author=author, per_page=per_page)

    async def commit_detail(sha: str) -> Any:
        """读单个提交的改动详情(文件+patch,有截断),深挖某次修复的真实改动面。"""
        return await client.commit_detail(owner, name, sha)

    async def search_issues(query: str, is_pr: bool = True, per_page: int = 20) -> Any:
        """仓内 issue/PR 搜索(is:pr 查贡献),query 可带 author:xxx。核对贡献声明。"""
        return await client.search_issues(owner, name, query, is_pr=is_pr, per_page=per_page)

    specs: list[tuple[Any, str]] = [
        (search_repos, "search_repos"),
        (list_user_repos, "list_user_repos"),
        (repo_meta, "repo_meta"),
        (list_files, "list_files"),
        (read_file, "read_file"),
        (search_in_repo, "search_in_repo"),
        (read_commits, "read_commits"),
        (commit_detail, "commit_detail"),
        (search_issues, "search_issues"),
    ]
    return [
        StructuredTool.from_function(fn, coroutine=fn, name=name_, description=fn.__doc__ or "")
        for fn, name_ in specs
    ]


async def explore_repo(
    *,
    project_text: str,
    owner: str,
    name: str,
    attribution: str,
    login: str,
    client: GitHubClient,
    model: Any,
    dossier: Dossier | None = None,
) -> Dossier:
    """跑一个仓的受限探索,返回 dossier(唯一产出)。

    GitHub 不可达不作失败:返回空 dossier(degraded=True),出题段降级
    guided(spec §3.4)。模型异常同理——探索段异常不炸 bundle。"""
    dossier = dossier or Dossier(attribution=attribution)
    tools = build_explore_tools(client, owner, name)
    tools_by_name = {t.name: t for t in tools}
    try:
        model = model.bind_tools(tools)
    except Exception:  # noqa: BLE001 — bind 失败等同探索失败,降级出题
        dossier.degraded = True
        dossier.degrade_reason = "模型工具绑定失败"
        return dossier

    system = SystemMessage(content=_explore_system_text())
    messages: list = [
        system,
        HumanMessage(
            "候选人项目自述:\n"
            f"{project_text}\n\n"
            f"锁定仓库:{owner}/{name}(归属: {attribution or '未标注'})"
            + (f",候选人 GitHub 登录名: {login}" if login else "")
            + "\n请开始探索:调工具收集十类取材材料,材料自认充分即停止调工具并简短总结。"
        ),
    ]

    start = time.monotonic()
    input_tokens = 0
    output_tokens = 0
    cache_hit = 0
    cache_miss = 0
    turn = 0
    try:
        while turn < MAX_TURNS:
            elapsed = time.monotonic() - start
            if elapsed >= MAX_WALL_SECONDS:
                dossier.degraded = True
                dossier.degrade_reason = dossier.degrade_reason or "墙钟 300s 触顶"
                break
            if dossier.degraded:  # dossier 40K 已触顶(add 里标记)
                break
            turn += 1
            # D8:步数计数放 user 消息尾部,追加不改写
            messages.append(
                HumanMessage(
                    f"[探索步 {turn}/{MAX_TURNS},已用 {int(elapsed)}s,"
                    f"dossier {dossier.total_chars} 字符]"
                )
            )
            remaining = MAX_WALL_SECONDS - (time.monotonic() - start)
            response = await asyncio.wait_for(model.ainvoke(messages), timeout=max(remaining, 1.0))
            messages.append(response)
            # D9/#154:extract_usage 统一解析。raw token_usage 优先——
            # DeepSeek prompt_cache_hit/miss 只在原始 usage,langchain 转换
            # 会丢(#113 先例,评审 P0 实测 usage_metadata 恒真值短路兜底)
            response_metadata = getattr(response, "response_metadata", None) or {}
            usage = extract_usage(
                response_metadata.get("token_usage") or getattr(response, "usage_metadata", None)
            )
            input_tokens += usage.get("input_tokens") or 0
            output_tokens += usage.get("output_tokens") or 0
            cache_hit += usage.get("cache_hit_tokens") or 0
            cache_miss += usage.get("cache_miss_tokens") or 0
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                break  # 材料自认充分,正常终止
            written_slots: list[str] = []
            for tc in tool_calls:
                # D7 硬闸:轮数 = LLM+工具调用累计,逐次判定(评审 P1:一批并行
                # 工具调用不得突破 80 上限);墙钟同样覆盖工具执行
                if turn >= MAX_TURNS:
                    dossier.degraded = True
                    dossier.degrade_reason = dossier.degrade_reason or f"轮数 {MAX_TURNS} 触顶"
                    break
                remaining = MAX_WALL_SECONDS - (time.monotonic() - start)
                if remaining <= 0:
                    dossier.degraded = True
                    dossier.degrade_reason = dossier.degrade_reason or "墙钟 300s 触顶"
                    break
                turn += 1
                tool_name = tc.get("name") or ""
                tool = tools_by_name.get(tool_name)
                payload = None  # 预绑定:首调用即炸时 isinstance 判定不炸
                if tool is None:
                    observation = f"未知工具 {tool_name}"
                else:
                    try:
                        payload = await asyncio.wait_for(
                            tool.coroutine(**(tc.get("args") or {})),
                            timeout=max(remaining, 1.0),
                        )
                        observation = _observation_text(tool_name, payload)
                    except TimeoutError:
                        dossier.degraded = True
                        dossier.degrade_reason = dossier.degrade_reason or "墙钟 300s 触顶"
                        break
                    except GitHubUnavailable as exc:
                        observation = f"工具不可用(降级信号): {exc}"
                    except Exception as exc:  # noqa: BLE001 — 单工具炸不炸整轮
                        observation = f"工具执行异常: {type(exc).__name__}: {exc}"
                if tool_name == "list_files" and isinstance(payload, tuple):
                    dossier.paths, dossier.paths_truncated = payload
                slots = _SLOT_BY_TOOL.get(tool_name, ())
                # 逐槽写入(any 会短路,多槽元组实际只进首槽——评审 P1)
                written = [dossier.add(slot, observation) for slot in slots] if observation else []
                if written:
                    hit = [
                        slot
                        for slot in slots
                        if slot in dossier.slots and observation in dossier.slots[slot]
                    ]
                    written_slots.extend(hit)
                # OpenAI 序纪律:ToolMessage 必须紧跟 assistant tool_calls 连续出现,
                # 中间不得插入其他角色消息(真机 400 实测)。
                # #163:模型面的工具返回过注入守卫(数据区标签+扫描);dossier
                # 仍存原始证据(出题材料不被标注污染)。
                guarded, _trace = guard_tool_result(tool_name, observation[:2000] or "(空结果)")
                messages.append(
                    ToolMessage(
                        guarded,
                        tool_call_id=tc.get("id") or "",
                        name=tool_name,
                    )
                )
            # D8:轮尾单条 user 消息承载观察回执+步数计数(追加不改写)
            receipt = (
                f"观察已写入 {'/'.join(written_slots)}"
                if written_slots
                else "观察未入槽(重复/超限/空)"
            )
            messages.append(
                HumanMessage(
                    f"[{receipt};探索步 {turn}/{MAX_TURNS},"
                    f"已用 {int(elapsed)}s,dossier {dossier.total_chars} 字符]"
                )
            )
        else:
            dossier.degraded = True
            dossier.degrade_reason = dossier.degrade_reason or f"轮数 {MAX_TURNS} 触顶"
    except GitHubUnavailable as exc:
        dossier.degraded = True
        dossier.degrade_reason = dossier.degrade_reason or f"GitHub 不可达: {exc}"
    except Exception as exc:  # noqa: BLE001 — 探索段异常不判任务失败(spec §3.4)
        dossier.degraded = True
        dossier.degrade_reason = dossier.degrade_reason or f"探索异常: {type(exc).__name__}"

    dossier.turns_used = turn
    dossier.input_tokens = input_tokens or None
    dossier.output_tokens = output_tokens or None
    dossier.cache_hit_tokens = cache_hit or None
    dossier.cache_miss_tokens = cache_miss or None
    return dossier


def _explore_system_text() -> str:
    """探索技能文本(ADR-0004 唯一权威是文件;investigate v3=探索段,D14)。"""
    from official_agent.prompt_loader import load_prompt

    return load_prompt("evaluation/explore.md")


async def run_explore(
    project_text: str,
    *,
    owner: str,
    name: str,
    attribution: str = "",
    login: str = "",
    github_base: str = "https://api.github.com",
    github_token: str = "",
) -> Dossier:
    """便捷入口:自建 client 与 model_strong(与 run_investigation 同构)。"""
    from official_agent.config import get_effective_settings
    from official_agent.graphs.assistant import build_model

    settings = get_effective_settings()
    model = build_model(settings, temperature=0.2)
    client = GitHubClient(base_url=github_base, token=github_token)
    return await explore_repo(
        project_text=project_text,
        owner=owner,
        name=name,
        attribution=attribution,
        login=login,
        client=client,
        model=model,
    )


# slot 名单的静态自检(防 SLOT_NAMES 演化时映射表漏更)
assert all(slot in SLOT_NAMES for slots in _SLOT_BY_TOOL.values() for slot in slots)
