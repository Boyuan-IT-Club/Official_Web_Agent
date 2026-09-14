"""调查子图·仓深挖测试:路由/值得度/GitHub 客户端(respx)/子图三路径。"""

import json
from typing import Any

import httpx
import pytest
import respx
from httpx import Response

from official_agent.evaluation import investigate as inv
from official_agent.evaluation import investigate_graph as ig
from official_agent.evaluation.github_client import GitHubClient, GitHubUnavailable

# ── 路由与值得度(纯函数) ────────────────────────────────


def test_extract_repo_tolerates_git_suffix_and_noise() -> None:
    assert inv.extract_repo("项目 https://github.com/aB_C/demo.git 报名页") == ("aB_C", "demo")
    assert inv.extract_repo("github.com/owner/repo,做了 X") == ("owner", "repo")
    assert inv.extract_repo("只有文字没有链接") is None


def test_extract_repos_returns_all_and_dedupes() -> None:
    """多仓候选不再只取第一个;保序+去重;.git/尾随标点清洗。"""
    text = (
        "前端 https://github.com/me/web.git 后端 github.com/me/api, 重复 https://github.com/me/web"
    )
    assert inv.extract_repos(text) == [("me", "web"), ("me", "api")]
    assert inv.extract_repos("没有链接") == []
    assert inv.extract_repos("github.com/owner/repo.") == [("owner", "repo")]
    # extract_repo 仍是首个(向后兼容)
    assert inv.extract_repo(text) == ("me", "web")


def test_route_has_repo_paths_unchanged() -> None:
    """有仓两条路径行为不变(不回归)。"""
    long_text = "我做了社团官网重构,负责报名页与后端接口。" * 3
    assert inv.route_project(long_text, True) == "deep_dive"  # 有仓可读
    assert inv.route_project("太短", False) == "guided"  # 有仓但不可读→引导


def test_route_no_repo_with_tech_stack_is_cv_dive() -> None:
    """无仓 + 抽得出技术栈 → cv_dive(简历本身有料,值得深挖)。"""
    assert inv.route_project("太短", None, tech_count=3) == "cv_dive"


def test_route_no_repo_with_substantive_project_is_cv_dive() -> None:
    """无仓 + 无技术栈但项目经验有实质内容 → cv_dive。"""
    text = "我做了社团官网重构,负责报名页与后端接口。"
    assert inv.route_project(text, None) == "cv_dive"


def test_route_no_repo_with_placeholder_only_is_guided() -> None:
    """无仓 + 无技术栈 + 项目经验全虚词 → guided(降级生效)。"""
    assert inv.route_project("目前没有做过什么项目。", None) == "guided"
    assert inv.route_project("暂无", None) == "guided"
    assert inv.route_project("没有项目经验", None) == "guided"


def test_route_empty_project_text_is_skip() -> None:
    """三栏皆空的简历:此维不出题,交给兜底组(不硬凑通用引导题)。"""
    assert inv.route_project("", None) == "skip"
    assert inv.route_project("   ", None) == "skip"


def test_route_short_but_specific_is_not_punished() -> None:
    """不用字数阈值:短但具体不算没料(阈值会误杀这类简历)。"""
    assert inv.route_project("用 Python 写了爬虫", None) == "cv_dive"


def test_route_tech_stack_wins_over_empty_project_text() -> None:
    """项目栏空但技术栈有料 → cv_dive(不是 skip)。"""
    assert inv.route_project("", None, tech_count=2) == "cv_dive"
    assert inv.route_project("暂无", None, tech_count=1) == "cv_dive"


def test_placeholder_only_keeps_real_content() -> None:
    """占位表述里夹着真实内容 → 有料(宁可判有料,别误打回兜底)。"""
    assert inv.route_project("目前没有做过什么项目,但自己写过爬虫", None) == "cv_dive"


# ── GitHub 客户端(respx) ────────────────────────────────


@respx.mock
async def test_client_happy_paths() -> None:
    base = "https://api.github.test"
    respx.get(f"{base}/repos/o/r").mock(return_value=Response(200, json={"default_branch": "main"}))
    respx.get(f"{base}/repos/o/r/readme").mock(return_value=Response(200, text="# Demo\n内容"))
    respx.get(f"{base}/repos/o/r/commits").mock(
        return_value=Response(
            200, json=[{"commit": {"message": "feat: x", "author": {"date": "2026-01-01"}}}]
        )
    )
    respx.get(f"{base}/repos/o/r/git/trees/main").mock(
        return_value=Response(
            200,
            json={
                "tree": [
                    {"type": "blob", "path": "src/app.py"},
                    {"type": "tree", "path": "src"},
                ]
            },
        )
    )
    client = GitHubClient(base_url=base)
    assert (await client.repo("o", "r"))["default_branch"] == "main"
    assert "Demo" in await client.readme("o", "r")
    commits = await client.commits("o", "r")
    assert commits[0]["message"] == "feat: x"
    paths, truncated = await client.tree_paths("o", "r", branch="main")
    assert paths == ["src/app.py"] and truncated is False


@respx.mock
async def test_client_unavailable_and_missing_readme() -> None:
    base = "https://api.github.test"
    respx.get(f"{base}/repos/o/private").mock(return_value=Response(404, json={}))
    respx.get(f"{base}/repos/o/noreadme/readme").mock(return_value=Response(404))
    respx.get(f"{base}/repos/o/noreadme").mock(
        return_value=Response(200, json={"default_branch": "main"})
    )
    client = GitHubClient(base_url=base)
    with pytest.raises(GitHubUnavailable):
        await client.repo("o", "private")
    assert await client.readme("o", "noreadme") == ""  # 无 README 是扣分项不是错误


@respx.mock
async def test_client_network_error_is_unavailable() -> None:
    base = "https://api.github.test"
    respx.get(f"{base}/repos/o/r").mock(side_effect=httpx.ConnectError("refused"))
    client = GitHubClient(base_url=base)
    with pytest.raises(GitHubUnavailable, match="连接失败"):
        await client.repo("o", "r")


# ── 子图三路径 ──────────────────────────────────────────


class _FakeGH:
    """可读仓:README 足量+12 提交+结构目录 → high 值得度。"""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        pass

    async def repo(self, owner, repo):
        return {"default_branch": "main"}  # route 探测;fetch 复用 branch

    async def readme(self, owner, repo):
        return "# Demo\n" + "x" * 600

    async def commits(self, owner, repo, per_page=30):
        return [{"message": f"feat: {i}", "date": "2026-01-01"} for i in range(12)]

    async def tree_paths(self, owner, repo, *, branch=None, limit=600):
        return ["README.md", "src/app.py", "tests/test_app.py"], False


def _q(anchor: str, path: str, text: str) -> str:
    return (
        f'{{"anchor": "{anchor}", "question": "{text}", "sub_prompts": [],'
        f'"answer_reference": {{"strong": "s", "acceptable": "a", "weak": "w"}},'
        f'"evidence": {{"path": "{path}", "note": "n"}}, "time_minutes": 3}}'
    )


def _entry_q(category: str, path: str, question: str) -> dict:
    return {
        "category": category,
        "question": question,
        "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
        "evidence": {"path": path, "note": "n"},
        "time_minutes": 3,
    }


def _reserve_q(category: str, path: str, question: str) -> dict:
    return {
        "category": category,
        "question": question,
        "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
        "evidence": {"path": path, "note": "n"},
        "time_minutes": 3,
    }


def _chain(category: str, theme: str, n_layers: int = 3) -> dict:
    return {
        "category": category,
        "theme": theme,
        "layers": [
            {
                "question": f"L{i + 1}: {theme} 的第{i + 1}层怎么落地?",
                "expected_signal": "能讲清设计取舍",
            }
            for i in range(n_layers)
        ],
    }


def _v2_payload(
    *,
    chains: list[dict] | None = None,
    reserves: list[dict] | None = None,
    entry: dict | None = None,
) -> str:
    """v2 题组 JSON(模型输出形状)。缺省=合规组:入口+2 链+2 备选。"""
    payload = {
        "repo_summary": "社团官网,活跃",
        "entry": entry or _entry_q("C1_背景与动机", "src/app.py", "为什么做这个项目?"),
        "chains": chains
        if chains is not None
        else [
            _chain("C4_实现细节拷打", "src/app.py 的请求处理链"),
            _chain("C7_边界与失败模式", "tests 目录覆盖的边界场景"),
        ],
        "reserves": reserves
        if reserves is not None
        else [
            _reserve_q("C6_难点与调试", "README.md", "最大的难点是什么,怎么排查的?"),
            _reserve_q("C9_变更条件", "src/app.py", "流量 ×10 哪里先坏?"),
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _install_fake_explore(monkeypatch, chars: int = 2000) -> None:
    """探索段替身:返回带材料与路径的 dossier,worthiness 由体量定。"""

    from official_agent.evaluation.dossier import Dossier

    def _fake_run_explore(project_text, **kw):
        async def _impl():
            d = Dossier(attribution=kw.get("attribution", ""))
            d.add("C1_背景与动机", "# Demo\n" + "x" * chars)
            d.add("C3_架构与数据流", "README.md src/app.py tests/test_app.py")
            d.paths = ["README.md", "src/app.py", "tests/test_app.py"]
            d.turns_used = 2
            return d

        return _impl()

    monkeypatch.setattr(ig, "run_explore", _fake_run_explore)


def _install_fake_gh_and_model(monkeypatch, payload: str, explore_chars: int = 2000) -> None:
    _install_fake_explore(monkeypatch, chars=explore_chars)
    monkeypatch.setattr(ig, "GitHubClient", _FakeGH)

    class _Msg:
        content = payload

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "get_effective_settings", _S)


@pytest.mark.asyncio
async def test_deep_dive_happy_path(monkeypatch) -> None:
    _install_fake_gh_and_model(monkeypatch, _v2_payload())
    qs = await ig.run_investigation("我做了 https://github.com/me/demo 报名页重构")
    assert qs["mode"] == "repo_deep_dive"
    assert qs["group"]["entry"]["evidence"]["path"] == "src/app.py"  # 路径真实在仓
    assert len(qs["group"]["chains"]) == 2  # 追问链 2-4


@pytest.mark.asyncio
async def test_probe_failure_degrades_to_guided(monkeypatch) -> None:
    """仓探测失败 → guided 降级,题不带仓路径(私有/不可达注明)。"""

    class _PrivateGH(_FakeGH):
        async def repo(self, owner, repo):
            raise GitHubUnavailable("GitHub 404")

    monkeypatch.setattr(ig, "GitHubClient", _PrivateGH)

    class _Msg:
        content = (
            '{"repo_summary": "",'
            '"questions": [{"anchor": "guided", "question": "项目里你承担了什么?",'
            '"sub_prompts": [],'
            '"answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},'
            '"evidence": {"path": "", "note": "仓不可读,通用引导"},'
            '"time_minutes": 3}]}'
        )

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ig, "get_effective_settings", _S)

    qs = await ig.run_investigation("我做了 github.com/me/private 电商后端,用了 Redis")
    assert qs["mode"] == "guided"
    assert qs["group"]["entry"]["evidence"]["path"] == ""
    assert qs["group"]["chains"] == []


@pytest.mark.asyncio
async def test_skip_path_no_model_call(monkeypatch) -> None:
    def _boom(*a, **k):
        raise AssertionError("skip 路径不得调模型/GitHub")

    monkeypatch.setattr(ig, "build_model", _boom)
    monkeypatch.setattr(ig, "GitHubClient", _boom)
    qs = await ig.run_investigation("   ")
    assert qs["mode"] == "skipped"
    assert qs["group"]["entry"] is None and qs["group"]["chains"] == []


@pytest.mark.asyncio
async def test_deep_dive_fabricated_path_rejected(monkeypatch) -> None:
    """证据路径不在仓内(编造)→ RuntimeError,可重试。"""
    payload = json.loads(_v2_payload())
    payload["entry"]["evidence"]["path"] = "src/编造的路径.py"
    _install_fake_gh_and_model(monkeypatch, json.dumps(payload, ensure_ascii=False))
    with pytest.raises(RuntimeError, match="不在仓内"):
        await ig.run_investigation("项目 https://github.com/me/demo")


def test_repo_regex_boundaries() -> None:
    """句点收尾/伪站名不误配不误粘。"""
    assert inv.extract_repo("项目是 github.com/owner/repo.") == ("owner", "repo")
    assert inv.extract_repo("看 mygithub.com/owner/repo 这个") is None
    assert inv.extract_repo("github.com/owner/repo.git 已归档") == ("owner", "repo")


@pytest.mark.asyncio
async def test_empty_dossier_degrades_to_guided(monkeypatch) -> None:
    """探索零材料 → guided 降级(GitHub 不可达/探索全败,替代旧 worthiness=none)。"""

    def _fake_run_explore(project_text, **kw):
        async def _impl():
            from official_agent.evaluation.dossier import Dossier

            d = Dossier()
            d.degraded = True
            d.degrade_reason = "GitHub 不可达"
            return d

        return _impl()

    monkeypatch.setattr(ig, "run_explore", _fake_run_explore)

    class _Msg:
        content = (
            '{"repo_summary": "",'
            '"questions": [{"anchor": "guided", "question": "项目里你承担了什么?",'
            '"sub_prompts": [],'
            '"answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},'
            '"evidence": {"path": "", "note": "仓不可读,通用引导"},'
            '"time_minutes": 3}]}'
        )

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "get_effective_settings", _S)
    qs = await ig.run_investigation("项目 https://github.com/me/bare 空仓")
    assert qs["mode"] == "guided"
    assert qs["group"]["entry"] is not None and qs["group"]["chains"] == []


@pytest.mark.asyncio
async def test_explore_midway_failure_degrades_to_guided(monkeypatch) -> None:
    """route 探测通过但探索段全败 → 降级 guided + 注明。"""

    def _fake_run_explore(project_text, **kw):
        async def _impl():
            from official_agent.evaluation.dossier import Dossier

            d = Dossier()
            d.degraded = True
            d.degrade_reason = "GitHub 不可达: GitHub 403"
            return d

        return _impl()

    monkeypatch.setattr(ig, "run_explore", _fake_run_explore)

    class _Msg:
        content = (
            '{"repo_summary": "",'
            '"questions": [{"anchor": "guided", "question": "自述的重构你承担了哪些?",'
            '"sub_prompts": [],'
            '"answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},'
            '"evidence": {"path": "", "note": "仓不可读,通用引导"},'
            '"time_minutes": 3}]}'
        )

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ig, "get_effective_settings", _S)

    qs = await ig.run_investigation("我做了 github.com/me/demo 官网重构,React 技术栈")
    assert qs["mode"] == "guided"
    assert qs["group"]["entry"]["evidence"]["path"] == ""


@pytest.mark.asyncio
async def test_chain_count_below_minimum_rejected(monkeypatch) -> None:
    """追问链 <2 → 结构校验拒绝,可重试。"""
    payload = json.loads(_v2_payload())
    payload["chains"] = payload["chains"][:1]
    _install_fake_gh_and_model(monkeypatch, json.dumps(payload, ensure_ascii=False))
    with pytest.raises(RuntimeError, match="追问链不足"):
        await ig.run_investigation("项目 https://github.com/me/demo")


async def test_deep_dive_allow_empty_path_with_note(monkeypatch) -> None:
    """纯取向题(无单一文件锚点)允许空路径,note 必填。"""
    entry = _entry_q("C1_背景与动机", "", "为什么选择这个方向?")
    entry["evidence"]["note"] = "纯取向题,跨多文件"
    payload = json.loads(_v2_payload(entry=entry))
    _install_fake_gh_and_model(monkeypatch, json.dumps(payload, ensure_ascii=False))
    qs = await ig.run_investigation("项目 https://github.com/me/demo " + "做了很多事 " * 5)
    assert qs["mode"] == "repo_deep_dive"
    assert qs["group"]["entry"]["evidence"]["path"] == ""  # 空路径+note 放行


@pytest.mark.asyncio
async def test_v2_categories_not_forced_uniform(monkeypatch) -> None:
    """v2:链条类别自由组合(十类 taxonomy),不要求均匀覆盖。"""
    chains = [
        _chain("C2_技术选型与权衡", "src/app.py 依赖清单的选型权衡"),
        _chain("C6_难点与调试", "tests/test_app.py 覆盖的边界场景"),
        _chain("C5_数字与规模", "README.md 声明的规模数字"),
    ]
    reserves = [_reserve_q("C10_复盘与改进", "README.md", "重做会改什么?")]
    payload = json.loads(_v2_payload(chains=chains, reserves=reserves))
    _install_fake_gh_and_model(monkeypatch, json.dumps(payload, ensure_ascii=False))
    qs = await ig.run_investigation("项目 https://github.com/me/demo " + "做了很多事 " * 5)
    assert qs["mode"] == "repo_deep_dive"
    cats = {c["category"] for c in qs["group"]["chains"]}
    assert cats == {
        "C2_技术选型与权衡",
        "C6_难点与调试",
        "C5_数字与规模",
    }


# ── 后置校验 ──


def test_adversarial_blacklist_rejects_all_forms() -> None:
    """对抗前提黑名单逐词生效(布尔优先级曾致 5 词死代码)。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    dossier_text = "README.md src/app.py tests/test_app.py"
    all_paths = ["src/app.py", "README.md", "tests/test_app.py"]
    for word in ("矛盾", "撒谎", "夸大", "打脸", "为什么没做到"):
        bad = json.loads(_v2_payload())
        bad["entry"]["question"] = f"你自述主导重构,和代码对不上,{word}了?"
        with pytest.raises(ValueError, match="对抗前提"):
            _validate_group_v2(bad, dossier_text, all_paths)


def test_legitimate_anchoring_question_passes() -> None:
    """简历锚定横切:合法「你自述里提到 X,为什么选它」不误拒。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = json.loads(_v2_payload())
    payload["entry"]["question"] = "你自述里提到用 Redis,为什么选它而不是 MySQL?"
    dossier_text = "README.md src/app.py tests/test_app.py,依赖清单含 redis 客户端"
    _validate_group_v2(payload, dossier_text, ["src/app.py", "README.md", "tests/test_app.py"])


def test_chain_source_not_in_dossier_rejected() -> None:
    """链源真实性:theme/层问题引用 dossier 没有的组件 → 拒绝。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = json.loads(_v2_payload())
    payload["chains"][0]["theme"] = "kafka 消息队列的削峰设计"
    with pytest.raises(ValueError, match="链源不在 dossier"):
        _validate_group_v2(
            payload,
            "README.md src/app.py,依赖只有 redis 与 flask",
            ["src/app.py", "README.md"],
        )


def test_chain_source_ignores_our_scaffolding_words() -> None:
    """链源校验忽略我们自己材料里的拴架词(模型会把材料标签抄进 theme)。

    实测:模型产出「源自 dossier C4 项目经验栏…」时,`dossier` 是我们材料
    标签里的内部用词、不在候选人材料中,按内容词判定会把真实链误拒。
    """
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = json.loads(_v2_payload())
    payload["chains"][0] = {
        "category": "C4_实现细节拷打",
        "theme": "源自 dossier C4 项目经验栏的报名页重构",
        "layers": [
            {"question": f"第{i}层怎么落地?", "expected_signal": "答到什么算过"}
            for i in range(3)
        ],
    }
    payload["chains"][1] = {
        "category": "C7_边界与失败模式",
        "theme": "flask 报名页的边界场景",
        "layers": [
            {"question": f"第{i}层怎么落地?", "expected_signal": "答到什么算过"}
            for i in range(3)
        ],
    }
    payload["entry"]["evidence"]["path"] = ""
    for r in payload["reserves"]:
        r["evidence"]["path"] = ""
    _validate_group_v2(payload, "档案:报名页重构,用了 flask", [], no_repo=True)


def test_chain_source_still_rejects_fabricated_component() -> None:
    """忽略表不得放宽成「什么都放行」:编造的组件词仍被拒。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = json.loads(_v2_payload())
    payload["chains"][0]["theme"] = "kafka 消息队列的削峰设计"
    with pytest.raises(ValueError, match="链源不在 dossier"):
        _validate_group_v2(payload, "档案:报名页重构,用了 flask", [], no_repo=True)

def test_reserve_path_whitelist_enforced() -> None:
    """备选题 evidence.path 白名单同样校验(曾只查 entry)。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = json.loads(_v2_payload())
    payload["reserves"][0]["evidence"]["path"] = "fabricated/不存在.py"
    with pytest.raises(ValueError, match="不在仓内"):
        _validate_group_v2(
            payload,
            "README.md src/app.py tests/test_app.py",
            ["src/app.py", "README.md", "tests/test_app.py"],
        )


@pytest.mark.asyncio
async def test_thin_dossier_over_limit_rejected(monkeypatch) -> None:
    """敷衍 dossier(体量 <400 字符)题量 >3 → 拒绝重试。"""
    payload = json.loads(_v2_payload())  # 缺省组 8 题 > 3
    _install_fake_gh_and_model(
        monkeypatch, json.dumps(payload, ensure_ascii=False), explore_chars=50
    )
    with pytest.raises(RuntimeError, match="敷衍"):
        await ig.run_investigation("项目 https://github.com/me/demo " + "说明 " * 10)


@pytest.mark.asyncio
async def test_generation_usage_into_envelope(monkeypatch) -> None:
    """出题段单次 usage → 信封 generation_usage。"""

    class _Msg:
        content = json.loads(_v2_payload())

    class _RawUsageModel:
        def bind_tools(self, tools: Any, **kwargs: Any):
            return self

        async def ainvoke(self, messages: list):
            from langchain_core.messages import AIMessage

            return AIMessage(
                json.dumps(_Msg.content, ensure_ascii=False),
                response_metadata={
                    "token_usage": {
                        "prompt_tokens": 900,
                        "completion_tokens": 120,
                        "prompt_cache_hit_tokens": 500,
                        "prompt_cache_miss_tokens": 400,
                    }
                },
            )

    _install_fake_explore(monkeypatch)
    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _RawUsageModel())

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "get_effective_settings", _S)
    qs = await ig.run_investigation("项目 https://github.com/me/demo " + "做了很多事 " * 5)
    gu = qs.get("generation_usage")
    assert gu is not None and gu["input_tokens"] == 900
    assert gu["cache_hit_tokens"] == 500


# ── cv_dive:无仓简历路径 ────────────────────────────────

#: 无仓简历的结构:技术栈单列一栏 + 项目经验有实质内容,无任何 GitHub 链接。
_CV_RESUME = (
    "技术能力\n技术栈:\nPython、PyTorch、ResNet\n"
    "项目经验:\n工业钢材缺陷检测,用 PyTorch 复现了 ResNet 分类\n"
    "自我介绍:\n喜欢折腾模型"
)


def _cv_chain(category: str, theme: str, question: str) -> dict:
    """链的文本须含 dossier 里出现过的拉丁词元(链源真实性校验)。"""
    return {
        "category": category,
        "theme": theme,
        "layers": [
            {"question": q, "expected_signal": "答到什么算过"}
            for q in (
                f"{question} 是什么?",
                f"{question} 在项目里怎么用的?",
                f"{question} 有什么坑?",
            )
        ],
    }


def _cv_payload() -> str:
    """CV 出题形状:入口 + 两条带链的技术栈题,证据锚是简历原文句。"""
    return json.dumps(
        {
            "repo_summary": "无仓简历:技术栈 Python/PyTorch/ResNet",
            "entry": {
                "category": "C2_技术选型与权衡",
                "question": "你的技术栈里为什么选 PyTorch?",
                "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
                "evidence": {"path": "", "note": "技术栈栏:Python、PyTorch、ResNet"},
                "time_minutes": 3,
            },
            "chains": [
                _cv_chain("C2_技术选型与权衡", "技术栈栏的 PyTorch", "PyTorch"),
                _cv_chain("C4_实现细节拷打", "项目里的 ResNet", "ResNet"),
            ],
            "reserves": [],
        },
        ensure_ascii=False,
    )


def _install_fake_cv_model(monkeypatch, payload: str) -> None:
    """简历路径:只有出题段调模型(技术栈抽取在路由前已由项目栏有料短路)。"""

    class _Msg:
        content = payload

    class _M:
        async def ainvoke(self, messages):
            return _Msg()

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ig, "get_effective_settings", _S)


@pytest.mark.asyncio
async def test_cv_dive_produces_question_chains(monkeypatch) -> None:
    """无仓简历出带追问链的题组,不再是 entry-only(本票核心)。"""
    _install_fake_cv_model(monkeypatch, _cv_payload())
    qs = await ig.run_investigation(_CV_RESUME)
    assert qs["mode"] == "cv_dive"
    assert len(qs["group"]["chains"]) == 2
    assert all(len(c["layers"]) == 3 for c in qs["group"]["chains"])


@pytest.mark.asyncio
async def test_cv_dive_dossier_carries_resume_text(monkeypatch) -> None:
    """取材档案用简历文本渲染,不再是「探索段未取得材料」。"""
    _install_fake_cv_model(monkeypatch, _cv_payload())
    qs = await ig.run_investigation(_CV_RESUME)
    dossier = qs["explore_meta"]
    assert dossier["dossier_chars"] > 0
    assert qs["group"]["entry"]["evidence"]["path"] == ""  # 无仓:不带仓路径


@pytest.mark.asyncio
async def test_cv_dive_repo_path_never_used(monkeypatch) -> None:
    """简历没有仓:模型若给仓内路径,那是臆造,必须拒。"""
    payload = json.loads(_cv_payload())
    payload["entry"]["evidence"]["path"] = "src/main.py"
    _install_fake_cv_model(monkeypatch, json.dumps(payload, ensure_ascii=False))
    with pytest.raises(RuntimeError, match="无仓路径不得带仓内"):
        await ig.run_investigation(_CV_RESUME)


@pytest.mark.asyncio
async def test_cv_dive_no_github_client(monkeypatch) -> None:
    """简历路径零 GitHub 调用(无仓可探)。"""

    def _boom(*a, **k):
        raise AssertionError("cv_dive 不得建 GitHub 客户端")

    _install_fake_cv_model(monkeypatch, _cv_payload())
    monkeypatch.setattr(ig, "GitHubClient", _boom)
    qs = await ig.run_investigation(_CV_RESUME)
    assert qs["mode"] == "cv_dive"


@pytest.mark.asyncio
async def test_cv_dive_not_thinned_by_short_resume(monkeypatch) -> None:
    """短简历是正常简历,不套用仓路径的体量阈值(误杀短但具体的简历)。"""
    _install_fake_cv_model(monkeypatch, _cv_payload())
    qs = await ig.run_investigation(_CV_RESUME)  # 档案体量远小于 400 字符
    assert len(qs["group"]["chains"]) == 2  # 未被判成敷衍而砍到 ≤3 题


@pytest.mark.asyncio
async def test_placeholder_resume_degrades_to_guided(monkeypatch) -> None:
    """「目前没有做过什么项目」→ guided 兜底,不硬凑深挖题。"""
    payload = json.dumps(
        {
            "repo_summary": "",
            "entry": {
                "category": "C1_背景与动机",
                "question": "讲讲你自己?",
                "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
                "evidence": {"path": "", "note": "通用引导"},
                "time_minutes": 3,
            },
            "chains": [],
            "reserves": [],
        },
        ensure_ascii=False,
    )
    _install_fake_cv_model(monkeypatch, payload)

    async def _no_tech(text):
        return []

    monkeypatch.setattr(ig, "extract_tech_stack", _no_tech)
    # 真实调用形状:project_text 只装项目栏;技术栈抽取返回空 → 判为没料
    qs = await ig.run_investigation("目前没有做过什么项目。")
    assert qs["mode"] == "guided"
    assert qs["group"]["chains"] == []
