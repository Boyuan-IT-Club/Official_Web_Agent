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

@pytest.mark.parametrize(
    "text",
    ["暂无", "无", "没有", "略", "同上", "没做过什么项目", "没什么项目", "暂无项目", "尚未参加"],
)
def test_route_placeholder_phrasings_are_guided(text: str) -> None:
    """票点名的「全虚词」写法逐条覆盖 → guided(有字但没料),不是 skip。"""
    assert inv.route_project(text, None) == "guided"


def test_route_real_content_is_never_mistaken_for_placeholder() -> None:
    """剥词算法不得把真实内容误判成占位(宁可判有料)。"""
    for text in ("用 Python 写了爬虫", "做了智慧停车小程序", "我参与了社团官网开发"):
        assert inv.route_project(text, None) == "cv_dive"


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

def _cv_payload_with(themes: list[str]) -> dict:
    """自足题组:链主题/层问题都不带仓内词元(供无仓校验用例)。"""
    return {
        "entry": {
            "category": "C1_背景与动机",
            "question": "讲讲报名页重构?",
            "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
            "evidence": {"path": "", "note": "档案"},
            "time_minutes": 3,
        },
        "chains": [
            {
                "category": "C4_实现细节拷打",
                "theme": theme,
                "layers": [
                    {"question": f"第{i}层怎么落地?", "expected_signal": "答到什么算过"}
                    for i in range(3)
                ],
            }
            for theme in themes
        ],
        "reserves": [],
    }


def test_chain_source_rejects_all_stopword_shell() -> None:
    """全由忽略词拼成的链没有可核对来源 → 拒绝(忽略表不得被抽干)。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = _cv_payload_with(["the and for with layer dossier"] * 2)
    with pytest.raises(ValueError, match="无可核对来源"):
        _validate_group_v2(payload, "dossier: 报名页重构,用了 flask", [])


def test_chain_source_rejects_plausible_english_fabrication() -> None:
    """英文编造词(非忽略词)仍走常规判定被拒 —— 忽略表收窄后的回归锚。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = _cv_payload_with(["resume candidate layer"] * 2)
    with pytest.raises(ValueError, match="链源不在 dossier"):
        _validate_group_v2(payload, "dossier: 报名页重构,用了 flask", [])


def test_chain_source_allows_chinese_theme_with_scaffolding_prefix() -> None:
    """中文链带拴架前缀仍放行:中文无法与 dossier 做词元比对(诚实边界)。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = _cv_payload_with(
        ["源自 dossier C4 项目经验栏的报名页重构", "源自 dossier 的项目数据流"]
    )
    _validate_group_v2(payload, "dossier: 报名页重构,用了 flask", [])


def test_chain_source_still_rejects_fabricated_component() -> None:
    """忽略表不得放宽成「什么都放行」:编造的组件词仍被拒。"""
    from official_agent.evaluation.investigate_graph import _validate_group_v2

    payload = json.loads(_v2_payload())
    payload["chains"][0]["theme"] = "kafka 消息队列的削峰设计"
    with pytest.raises(ValueError, match="链源不在 dossier"):
        _validate_group_v2(payload, "dossier: 报名页重构,用了 flask", [])

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


def _cv_chain(category: str, theme: str) -> dict:
    """技术链:theme = 技术名词原文(校验依据),每层带三档参考答案。"""
    return {
        "category": category,
        "theme": theme,
        "layers": [
            {
                "question": q,
                "expected_signal": "答到什么算过",
                "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
            }
            for q in (
                f"{theme} 是什么?",
                f"{theme} 在项目里怎么用的?",
                f"{theme} 有什么坑?",
            )
        ],
    }


def _cv_payload() -> str:
    """CV 出题形状:入口 + 两条技术链(theme=技术名词原文)+ 三档答案。"""
    return json.dumps(
        {
            "repo_summary": "无仓简历:技术栈 Python/PyTorch/ResNet",
            "entry": {
                "category": "C1_背景与动机",
                "question": "你的技术栈里最想聊哪个?",
                "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
                "evidence": {"path": "", "note": "技术栈栏:Python、PyTorch、ResNet"},
                "time_minutes": 3,
            },
            "chains": [
                _cv_chain("C2_技术选型与权衡", "PyTorch"),
                _cv_chain("C4_实现细节拷打", "ResNet"),
            ],
            "reserves": [],
        },
        ensure_ascii=False,
    )


def _install_fake_cv_model(monkeypatch, payload: str, *, tech: list[dict] | None = None) -> None:
    """简历路径替身:技术栈抽取与出题都走模型,分别打桩。"""
    from official_agent.evaluation.tech_stack import TechStackItem

    items = [
        TechStackItem(
            name=str(t["name"]),
            raw_text=str(t.get("raw_text", t["name"])),
            claimed_level=t.get("claimed_level", "listed"),
            used_in=tuple(t.get("used_in", ())),
        )
        for t in (
            tech
            if tech is not None
            else [
                {"name": "PyTorch", "raw_text": "Python、PyTorch、ResNet"},
                {"name": "ResNet", "raw_text": "用 PyTorch 复现了 ResNet 分类"},
            ]
        )
    ]

    async def _fake_extract(text: str):
        return items

    monkeypatch.setattr(ig, "extract_tech_stack", _fake_extract)

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


# ── CV 出题(技术栈题组 + 三档答案 + 防编造)──────────────


def _multi_tech_payload(names: list[str]) -> str:
    """每个技术名词一条链(每层带三档答案)—— AC 要求的形状。"""
    return json.dumps(
        {
            "repo_summary": "无仓简历",
            "entry": {
                "category": "C1_背景与动机",
                "question": "最想聊哪项技术?",
                "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
                "evidence": {"path": "", "note": "技术栈栏"},
                "time_minutes": 3,
            },
            "chains": [_cv_chain("C2_技术选型与权衡", n) for n in names],
            "reserves": [],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_strong_claim_techs_get_chains_weak_ones_do_not(monkeypatch) -> None:
    """技术栈按声称强度分配题量:强的开链深挖,弱的只出概念题。

    这不是偷懒:总量硬顶 15 题,每个技术都开 3 层链必然越界。把深度给
    声称最强的技术,也正是「提问深度与声称强度匹配」的落法。
    """
    names = ["Python", "PyTorch", "ResNet"]
    resume = f"技术栈:\n{'、'.join(names)}\n项目经验:\n用 PyTorch 复现了 ResNet 分类"
    tech = [{"name": n, "raw_text": f"技术栈:{'、'.join(names)}"} for n in names]
    _install_fake_cv_model(monkeypatch, _multi_tech_payload(names), tech=tech)
    qs = await ig.run_investigation("用 PyTorch 复现了 ResNet 分类", resume_text=resume)
    themes = [c["theme"] for c in qs["group"]["chains"]]
    assert themes == names  # 三个强声称技术各一条链
    assert qs["group"]["chains"][0]["layers"][0]["answer_reference"]["acceptable"]
    assert ig.validate_qbank_v2_group  # 走的是同一台校验机器


@pytest.mark.asyncio
async def test_all_techs_as_chains_over_capacity_rejected(monkeypatch) -> None:
    """每个技术都开链会超容量 —— 拒绝而不是硬塞(容量与题量硬顶双重把关)。"""
    names = ["Python", "PyTorch", "ResNet", "CNN", "YOLO"]
    resume = f"技术栈:\n{'、'.join(names)}\n项目经验:\n用 PyTorch 做了检测"
    tech = [{"name": n, "raw_text": f"技术栈:{'、'.join(names)}"} for n in names]
    _install_fake_cv_model(monkeypatch, _multi_tech_payload(names), tech=tech)
    with pytest.raises(RuntimeError, match="at most 4 items"):
        await ig.run_investigation("用 PyTorch 做了检测", resume_text=resume)


@pytest.mark.asyncio
async def test_tech_chain_layers_carry_three_tier_answers(monkeypatch) -> None:
    """每道技术栈题都带三档参考答案(面试官不熟悉该技术时的判断依据)。"""
    _install_fake_cv_model(monkeypatch, _cv_payload())
    qs = await ig.run_investigation(_CV_RESUME)
    for chain in qs["group"]["chains"]:
        for layer in chain["layers"]:
            ref = layer["answer_reference"]
            assert set(ref) == {"strong", "acceptable", "weak"}


@pytest.mark.asyncio
async def test_cv_prompt_version_reflects_cv_prompt(monkeypatch) -> None:
    """信封的 prompt_version 必须是本次实际用过的 prompt 版本(ADR-0004)。

    两条路径各有 prompt;写错版本会让改 prompt 后的评测归因比错对象。
    """
    _install_fake_cv_model(monkeypatch, _cv_payload())
    qs = await ig.run_investigation(_CV_RESUME)
    assert qs["prompt_version"] == ig._prompt_version(ig.CV_PROMPT_FILE)
    assert "cv_dive" in qs["prompt_version"]



def test_cv_prompt_chain_budget_matches_capacity() -> None:
    """prompt 的链条数约束必须与信封容量一致,且技术链/项目链受**共同**上限约束。

    出题是**整组原子**的:链条数超了校验直接拒整组,候选人一道题都拿不到。
    曾经 prompt 开头写「链最多 4 条」,而 A 段许可技术链 1-3 条、B 段许可
    项目链最多 2 个 —— 分段照做能到 5 条,模型如实执行就被拒;再叠加一句
    「出题宁多勿少」,等于主动往坑里推。真实简历上实测约 1/4 的简历因此
    零题产出。

    钉两件事:①prompt 明示的 chains 上限 = schema 的 MAX_CHAINS;
    ②存在一句把 A/B 绑在一起的共同上限(只说分头上限是不够的)。
    """
    import re

    from official_agent.evaluation.schema import MAX_CHAINS
    from official_agent.prompt_loader import load_prompt

    text = load_prompt(ig.CV_PROMPT_FILE)

    # ① 明示的 chains 上限须与信封容量同值(写错值等于照错值出题)
    assert re.search(rf"chains\s*2-{MAX_CHAINS}\s*条", text), "prompt 未明示 chains 上限"

    # ② 分头上限之和可以超过总上限(这是事实),故必须有共同上限兜住
    assert re.search(rf"条数之和\s*≤\s*{MAX_CHAINS}", text), (
        f"prompt 缺少技术链与项目链的共同上限(≤ {MAX_CHAINS})"
    )

    # ③ 不得再出现鼓励堆题的措辞(它正是超生成的直接推手)
    assert "宁多勿少" not in text, "prompt 仍含鼓励堆题的措辞"


def test_cv_prompt_budget_counts_reserves() -> None:
    """题量预算必须给出**含备选**的等式,而不是只算链。

    总题量硬顶是「入口 + 链层 + 备选」共享的池子;只按「4 条链 × 3 层 + 入口」
    算预算会漏掉备选,链写满 5 层时必然爆顶(实测过 18 > 15 的整组被拒)。
    """
    import re

    from official_agent.prompt_loader import load_prompt

    text = load_prompt(ig.CV_PROMPT_FILE)
    # 预算等式须同时出现三个加项,把「共享池子」这件事说清
    assert re.search(r"入口\s*1\s*\+[^\n]*链[^\n]*\+[^\n]*备选[^\n]*≤\s*15", text), (
        "prompt 的题量预算等式未把备选计入(链与备选共享 15 题硬顶)"
    )


@pytest.mark.asyncio
async def test_cv_material_keeps_resume_dossier(monkeypatch) -> None:
    """技术栈清单是**追加**到档案上的,不能把简历材料挤掉。

    项目深挖链靠档案里的项目栏/自我介绍/实习经历取材;替换掉档案等于让
    这类链失去素材来源,而校验与计量还在照常算,属静默丢失。
    """
    seen: list[str] = []

    class _Msg:
        content = _cv_payload()

    class _M:
        async def ainvoke(self, messages):
            seen.append(str(messages[0].content))
            return _Msg()

    class _S:
        model_strong = "test-strong"

    tech = [{"name": "PyTorch", "raw_text": "技术栈:PyTorch"}]
    _install_fake_cv_model(monkeypatch, _cv_payload(), tech=tech)
    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())
    await ig.run_investigation(_CV_RESUME)
    sent = seen[0]
    assert "技术栈清单" in sent  # 技术清单在
    assert "dossier 材料" in sent  # 档案也在(没被挤掉)
    assert "工业钢材缺陷检测" in sent  # 档案里确实是简历原文


@pytest.mark.asyncio
async def test_generate_retries_once_on_validation_error(monkeypatch) -> None:
    """校验不过时把错误回灌让模型自纠 —— 一次失手不该让整份简历无题。

    首次故意给 5 条链(超容量),第二次给合规输出;应产出合规题组而非报错。
    """
    bad = _multi_tech_payload([f"T{i}" for i in range(5)])
    resumes = f"技术栈:\n{'、'.join(f'T{i}' for i in range(5))}"
    tech = [{"name": f"T{i}", "raw_text": resumes} for i in range(5)]
    calls: list[str] = []

    class _Msg:
        def __init__(self, content: str) -> None:
            self.content = content

    class _M:
        async def ainvoke(self, messages):
            calls.append(str(messages[0].content))
            return _Msg(bad if len(calls) == 1 else _multi_tech_payload(["T0", "T1"]))

    class _S:
        model_strong = "test-strong"

    async def _fake_extract(text):
        from official_agent.evaluation.tech_stack import TechStackItem

        return [
            TechStackItem(name=t["name"], raw_text=t["raw_text"], claimed_level="listed")
            for t in tech
        ]

    monkeypatch.setattr(ig, "extract_tech_stack", _fake_extract)
    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ig, "get_effective_settings", _S)
    qs = await ig.run_investigation(resumes, resume_text=resumes)
    assert len(calls) == 2  # 重试了一次
    assert "不合规" in calls[1]  # 错误被回灌
    assert len(qs["group"]["chains"]) == 2


def test_fabricated_tech_theme_rejected() -> None:
    """链声明问的是简历里没有的技术 → 拒绝(防编造的主要落点)。

    链的 theme 是「这条链问哪项技术/哪个项目」的**声明**,可以拿简历原文
    逐字核对,所以这里必须卡死。
    """
    from official_agent.evaluation.investigate_graph import validate_qbank_v2_group

    payload = json.loads(_multi_tech_payload(["Python", "Kubernetes"]))
    payload["entry"]["evidence"]["path"] = ""
    with pytest.raises(ValueError, match="Kubernetes"):
        validate_qbank_v2_group(payload, "技术栈:Python、PyTorch", paths=[], no_repo=True)


def test_question_text_may_use_terms_absent_from_resume() -> None:
    """题面**正文**不按词元核对 —— 这是有意的边界,不是遗漏。

    候选人写「卷积神经网络」,面试官题面写「CNN」;候选人写「Git」,题面问
    「commit 粒度」——这些都是正当表述。按词元硬卡会把真实简历判成编造
    (实测会把 5/5 全部拦死),所以正文只受对抗前提黑名单约束,技术真实性
    由链 theme 与抽取器的原文闸门保证。
    """
    from official_agent.evaluation.investigate_graph import validate_qbank_v2_group

    payload = json.loads(_multi_tech_payload(["Python", "PyTorch"]))
    payload["entry"]["question"] = "CNN 和全连接的区别是什么?"
    payload["entry"]["evidence"]["path"] = ""
    payload["reserves"] = [
        {
            "category": "C8_真实性与贡献边界",
            "question": "你 commit 的粒度是怎么把握的?",
            "answer_reference": {"strong": "s", "acceptable": "a", "weak": "w"},
            "evidence": {"path": "", "note": "n"},
            "time_minutes": 3,
        }
    ]
    validate_qbank_v2_group(payload, "技术栈:Python、PyTorch", paths=[], no_repo=True)


def test_chain_layer_count_out_of_range_rejected() -> None:
    """链层数越界 → 拒绝(不传档位即标准档)。

    层数下界由**深度档**决定(标准档 3-5),schema 只留形状上界;故这里
    走的是默认档的判定路径,同时钉住「既有调用方不传档位时行为不变」。
    """
    from official_agent.evaluation.investigate_graph import validate_qbank_v2_group

    payload = json.loads(_multi_tech_payload(["Python", "PyTorch"]))
    payload["chains"][0]["layers"] = payload["chains"][0]["layers"][:2]
    payload["entry"]["evidence"]["path"] = ""
    with pytest.raises(ValueError):
        validate_qbank_v2_group(payload, "技术栈:Python、PyTorch", paths=[], no_repo=True)


def test_more_than_six_chains_rejected() -> None:
    """技术名词有上限:超出信封容量的链被拒。"""
    from official_agent.evaluation.schema import QuestionGroupV2

    payload = json.loads(_multi_tech_payload([f"T{i}" for i in range(7)]))
    with pytest.raises(ValueError):
        QuestionGroupV2.model_validate(
            {"entry": payload["entry"], "chains": payload["chains"], "reserves": []}
        )


def test_deep_dive_chain_layer_still_optional_answer_reference() -> None:
    """仓深挖的链层不强制三档答案 —— 不回归既有 deep_dive 出题形状。"""
    from official_agent.evaluation.investigate_graph import validate_qbank_v2_group

    payload = json.loads(_v2_payload())  # 链层只有 question+expected_signal
    validate_qbank_v2_group(
        payload,
        "README.md src/app.py tests/test_app.py",
        paths=["src/app.py", "README.md", "tests/test_app.py"],
    )


# ── 年级分档:按年级调出题深度 ─────────────────────────


def test_grade_band_maps_freshman_and_defaults_standard() -> None:
    """年级 → 深度档:大一单独一档,其余(含缺失)一律标准档。

    缺失必须有安全默认:分档拿不到年级时不能报错,也不能更严;标准档正是
    引入档位之前的既有行为,故不涉及年级的调用方行为不变。
    """
    assert inv.grade_band("大一") == "freshman"
    assert inv.grade_band("  大一 ") == "freshman"
    assert inv.grade_band("大二") == "standard"
    assert inv.grade_band("大三") == "standard"
    assert inv.grade_band("") == "standard"  # 缺字段
    assert inv.grade_band("研究生") == "standard"  # 认不出 → 标准,不是更严


def test_freshman_short_chain_accepted_standard_rejected() -> None:
    """同一条两层链:大一档收,标准档拒。

    层数下界是**深度策略**而非形状,故必须先过 schema(下界 1)再由语义校验
    按档判——否则大一短链会在模型层就被钉死,分档根本走不到。
    """
    payload = json.loads(_multi_tech_payload(["Python", "PyTorch"]))
    for chain in payload["chains"]:
        chain["layers"] = chain["layers"][:2]
    payload["entry"]["evidence"]["path"] = ""
    dossier = "技术栈:Python、PyTorch"

    group = ig.validate_qbank_v2_group(
        payload, dossier, paths=[], no_repo=True, grade_band="freshman"
    )
    assert all(len(c.layers) == 2 for c in group.chains)

    with pytest.raises(ValueError, match="链层数越界"):
        ig.validate_qbank_v2_group(
            payload, dossier, paths=[], no_repo=True, grade_band="standard"
        )


def test_freshman_band_rejects_five_layers() -> None:
    """大一档**上限也收紧**:5 层对大一就是问太深了。"""
    payload = json.loads(_multi_tech_payload(["Python", "PyTorch"]))
    for chain in payload["chains"]:
        layer = chain["layers"][0]
        chain["layers"] = [dict(layer) for _ in range(5)]
    payload["entry"]["evidence"]["path"] = ""
    with pytest.raises(ValueError, match="链层数越界"):
        ig.validate_qbank_v2_group(
            payload, "技术栈:Python、PyTorch", paths=[], no_repo=True, grade_band="freshman"
        )


def test_grade_band_default_keeps_standard_behavior() -> None:
    """不传 grade_band 时等于标准档 —— 既有调用方(仓深挖/探针)行为不变。"""
    payload = json.loads(_multi_tech_payload(["Python", "PyTorch"]))
    payload["entry"]["evidence"]["path"] = ""
    ig.validate_qbank_v2_group(payload, "技术栈:Python、PyTorch", paths=[], no_repo=True)


def _cv_payload_with_layers(depth: int) -> str:
    """按档位深浅生成题组:每个技术一条链,各 depth 层。"""
    payload = json.loads(_multi_tech_payload(["Python", "PyTorch"]))
    for chain in payload["chains"]:
        chain["layers"] = [dict(chain["layers"][0]) for _ in range(depth)]
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_grade_band_reaches_material_but_grade_text_does_not(monkeypatch) -> None:
    """档位进材料,**年级原文不进** —— 「年级」栏并不干净。

    这一栏的语义是年级,但后端记录过线上把它填成姓名;把原文灌进 prompt
    等于把一个未受信的字段当材料用。只给派生的档位标签。
    """
    seen: list[str] = []
    payloads = {"大一": _cv_payload_with_layers(2), "大二": _cv_payload_with_layers(3)}
    current = {"key": "大一"}

    class _Msg:
        response_metadata = {"token_usage": {}}

        def __init__(self, content: str) -> None:
            self.content = content

    class _Model:
        async def ainvoke(self, messages, *a, **k):
            seen.append(messages[0].content)
            return _Msg(payloads[current["key"]])

    _install_fake_cv_model(monkeypatch, _cv_payload(), tech=[{"name": "Python"}])
    # 必须在 _install_fake_cv_model 之后打桩:它会一并替换 build_model
    monkeypatch.setattr(ig, "build_model", lambda *a, **k: _Model())

    # 只认材料段的标记:prompt 正文本身也提到 «标准» 这个词,整串搜会误判
    await ig.run_investigation(_CV_RESUME, grade="大一")
    material = seen[-1].rsplit("技术栈清单:", 1)[-1]
    assert "出题深度档:大一" in material
    assert "出题深度档:标准" not in material

    # 年级栏不干净:线上被填成姓名时,原文绝不能出现在材料里(只出档位标签)
    seen.clear()
    current["key"] = "大一"
    await ig.run_investigation(_CV_RESUME, grade="大一 张三")
    assert "张三" not in seen[-1]
    assert "出题深度档:大一" in seen[-1]

    seen.clear()
    current["key"] = "大二"
    await ig.run_investigation(_CV_RESUME, grade="大二")
    assert "出题深度档:标准" in seen[-1].rsplit("技术栈清单:", 1)[-1]


@pytest.mark.asyncio
async def test_freshman_band_does_not_break_repo_path(monkeypatch) -> None:
    """年级档**不得**套到仓路径上 —— 那会形成「prompt 要 3-5 层、校验只收
    1-2 层」的死结:两次重试都不合规,整份候选人一题都拿不到。

    触发条件是「大一 + 有可读仓」:年级在简历侧取到、档位传到子图,但这条
    路由走的是仓深挖,它的 prompt 没有档位段、层数要求仍是 3-5。
    """
    _install_fake_gh_and_model(monkeypatch, _v2_payload())

    qs = await ig.run_investigation(
        "项目 https://github.com/me/demo", grade="大一"
    )
    assert qs["mode"] == "repo_deep_dive"
    assert len(qs["group"]["chains"]) == 2
    assert all(len(c["layers"]) == 3 for c in qs["group"]["chains"])


def test_chain_layer_rejects_invented_evidence_field() -> None:
    """链层多写一个键(如模型把出处塞进 `evidence_note`)→ 整组被拒。

    真实简历上实测过:prompt 只在「铁律」里说「用 evidence.note 写这句题的
    出处」,没说它**只属于 entry 与 reserves**;模型于是给链层也加了出处字段,
    而链层是 extra="forbid"。两次重试都撞同一处,整份候选人零题产出。

    这里钉住「链层是严格三键」这一契约:多一个键就不该被接受 —— 靠 prompt
    写清楚来避免,而不是放宽 schema。
    """
    payload = json.loads(_multi_tech_payload(["Python", "PyTorch"]))
    payload["chains"][0]["layers"][0]["evidence_note"] = "技术栈栏:Python"
    payload["entry"]["evidence"]["path"] = ""
    with pytest.raises(ValueError, match="evidence_note"):
        ig.validate_qbank_v2_group(
            payload, "技术栈:Python、PyTorch", paths=[], no_repo=True
        )


def test_cv_prompt_scopes_evidence_to_entry_and_reserves() -> None:
    """prompt 必须写明 evidence 的归属,并点出链层没有这个字段。

    这是上一个缺陷的成因:规则没写范围,模型就把它套到链层上。
    """
    from official_agent.prompt_loader import load_prompt

    text = load_prompt(ig.CV_PROMPT_FILE)
    assert "evidence` 只属于 entry 与 reserves" in text
    assert "链层没有这个字段" in text
