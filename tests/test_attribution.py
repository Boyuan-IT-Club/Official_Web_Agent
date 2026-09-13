"""B-AG2 入口瀑布与归属四级测试(#150;spec §3.1 + ADR-0008)。

respx fake 仓;四级各一例 + fork 参与判定 + 搜索撞名降级 + 贡献声明 +
route_node 集成。
"""

import respx
from httpx import Response

from official_agent.evaluation import investigate_graph as ig
from official_agent.evaluation.attribution import (
    attribute,
    detect_contribution_target,
    keyword_candidates,
    resolve_entry,
)
from official_agent.evaluation.github_client import GitHubClient

base = "https://api.github.test"


def _client() -> GitHubClient:
    return GitHubClient(base_url=base, token="tok")


def _json(payload) -> Response:
    return Response(200, json=payload)


# ── 四级判定 ─────────────────────────────────────────────


@respx.mock
async def test_trusted_own_when_owner_equals_login() -> None:
    result = await attribute("me", "my-project", login="me", source="url", client=_client())
    assert result.level == "trusted-own"
    assert result.deep_dive_allowed


@respx.mock
async def test_trusted_contribution_via_commits() -> None:
    respx.get(f"{base}/repos/org/other/commits").mock(
        _json([{"sha": "a1", "commit": {"message": "x"}, "author": {"login": "me"}}])
    )
    result = await attribute("org", "other", login="me", source="search", client=_client())
    assert result.level == "trusted-contribution"
    assert "commits author=me" in result.evidence


@respx.mock
async def test_trusted_contribution_via_pr_when_no_commits() -> None:
    respx.get(f"{base}/repos/org/other/commits").mock(_json([]))  # D3:无领先 commit
    respx.get(f"{base}/search/issues").mock(
        _json({"items": [{"number": 7, "title": "feat", "pull_request": {"url": "u"}}]})
    )
    result = await attribute("org", "other", login="me", source="search", client=_client())
    assert result.level == "trusted-contribution"
    assert "PR author=me" in result.evidence


async def test_claimed_url_without_login() -> None:
    result = await attribute("someone", "repo", login="", source="url", client=_client())
    assert result.level == "claimed"
    assert result.deep_dive_allowed  # claimed 深挖,信封标注


@respx.mock
async def test_unverified_search_hit_without_evidence() -> None:
    # 绑定了登录名但 commits/PR 都查无 → 搜索来源归 unverified
    respx.get(f"{base}/repos/org/popular/commits").mock(_json([]))
    respx.get(f"{base}/search/issues").mock(_json({"items": []}))
    result = await attribute("org", "popular", login="me", source="search", client=_client())
    assert result.level == "unverified"
    assert result.deep_dive_allowed is False  # 不深挖,仅 guided


# ── fork 参与判定(D3:不要求领先父仓) ───────────────────


@respx.mock
async def test_fork_under_other_owner_with_authored_commits() -> None:
    """上游仓的 fork 参与者:author=本人 commits 即 trusted-contribution,
    不检查 ahead(贡献可能已合并上游)。"""
    respx.get(f"{base}/repos/upstream/proj/commits").mock(
        _json([{"sha": "f1", "commit": {"message": "fix"}, "author": {"login": "me"}}])
    )
    result = await attribute("upstream", "proj", login="me", source="url", client=_client())
    assert result.level == "trusted-contribution"


# ── 入口瀑布 ─────────────────────────────────────────────


@respx.mock
async def test_waterfall_url_direct_wins() -> None:
    result = await resolve_entry(
        "我做了 XX 系统 https://github.com/me/xx-system",
        login="me",
        client=_client(),
    )
    assert result is not None and result.level == "trusted-own"
    assert (result.owner, result.name) == ("me", "xx-system")


@respx.mock
async def test_waterfall_bound_match_before_search() -> None:
    """无 URL:绑定名下仓按关键词匹配命中 → trusted-own,不触发搜索。"""
    respx.get(f"{base}/users/me/repos").mock(
        _json(
            [
                {
                    "name": "shop-mall",
                    "description": "电商中台",
                    "owner": {"login": "me"},
                    "fork": False,
                }
            ]
        )
    )
    search = respx.get(f"{base}/search/repositories").mock(_json({"items": []}))
    result = await resolve_entry(
        "项目名 shop-mall,做了订单与库存模块", login="me", client=_client()
    )
    assert result is not None and result.level == "trusted-own"
    assert search.call_count == 0  # 绑定匹配命中就不搜索


@respx.mock
async def test_waterfall_name_collision_degrades_to_unverified() -> None:
    """搜索兜底撞名:命中别人的同名仓 → unverified → 不深挖只 guided。"""
    respx.get(f"{base}/users/me/repos").mock(_json([]))  # 绑定名下无匹配
    respx.get(f"{base}/repos/stranger/blog/commits").mock(_json([]))
    respx.get(f"{base}/search/issues").mock(_json({"items": []}))
    respx.get(f"{base}/search/repositories").mock(
        _json(
            {
                "items": [
                    {
                        "name": "blog",
                        "full_name": "stranger/blog",
                        "description": "a blog engine",
                        "owner": {"login": "stranger"},
                        "fork": False,
                    }
                ]
            }
        )
    )
    result = await resolve_entry("我写了 blog 系统", login="me", client=_client())
    assert result is not None
    assert result.level == "unverified"
    assert (result.owner, result.name) == ("stranger", "blog")
    assert result.deep_dive_allowed is False


async def test_waterfall_no_keywords_returns_none() -> None:
    result = await resolve_entry("我用中文做了一个管理系统", login="", client=_client())
    assert result is None  # 提不出关键词 → guided/skip,不硬猜


# ── 贡献声明(D4) ────────────────────────────────────────


def test_detect_contribution_target() -> None:
    assert detect_contribution_target("给 kubernetes/kubernetes 贡献了调度器代码") == (
        "kubernetes",
        "kubernetes",
    )
    assert detect_contribution_target("contribute to vuejs/core") == ("vuejs", "core")
    assert detect_contribution_target("独立开发了一个博客") is None


@respx.mock
async def test_contribution_claim_unbound_is_claimed_not_deep() -> None:
    """未绑定:贡献声明只能 claimed 型过程题,不做仓内深挖(ADR-0008)。"""
    result = await attribute(
        "kubernetes", "kubernetes", login="", source="contribution", client=_client()
    )
    assert result.level == "claimed"
    assert result.deep_dive_allowed is False


@respx.mock
async def test_contribution_claim_bound_without_evidence_is_claimed() -> None:
    respx.get(f"{base}/repos/org/x/commits").mock(_json([]))
    respx.get(f"{base}/search/issues").mock(_json({"items": []}))
    result = await attribute("org", "x", login="me", source="contribution", client=_client())
    assert result.level == "claimed"
    assert "未查到" in result.evidence


# ── route_node 集成 ──────────────────────────────────────


@respx.mock
async def test_route_node_unverified_repo_routes_guided() -> None:
    """瀑布全路径:无 URL → 绑定名下无匹配 → 搜索撞名 → unverified → guided。"""
    respx.get(f"{base}/users/me/repos").mock(_json([]))  # 绑定名下无匹配
    respx.get(f"{base}/search/repositories").mock(
        _json(
            {
                "items": [
                    {
                        "name": "popular",
                        "full_name": "stranger/popular",
                        "description": "p",
                        "owner": {"login": "stranger"},
                        "fork": False,
                    }
                ]
            }
        )
    )
    respx.get(f"{base}/repos/stranger/popular/commits").mock(_json([]))
    respx.get(f"{base}/search/issues").mock(_json({"items": []}))
    respx.get(f"{base}/repos/stranger/popular").mock(_json({"default_branch": "main"}))
    state = {
        "project_text": "参与 popular 项目开发",
        "github_base": base,
        "candidate_login": "me",
    }
    result = await ig.route_node(state)
    assert result["route"] == "guided"  # 撞名仓可读也不深挖
    assert result["attribution"]["level"] == "unverified"
    assert result["repo_owner"] == "stranger"


@respx.mock
async def test_route_node_trusted_url_routes_deep_dive() -> None:
    respx.get(f"{base}/repos/me/own").mock(_json({"default_branch": "main"}))
    state = {
        "project_text": "项目 https://github.com/me/own 做了很多事" * 3,
        "github_base": base,
        "candidate_login": "me",
    }
    result = await ig.route_node(state)
    assert result["route"] == "deep_dive"  # substantive 文本 + trusted → 深挖
    assert result["attribution"]["level"] == "trusted-own"
    assert result["repo_owner"] == "me"


# ── 关键词提取 ───────────────────────────────────────────


def test_keyword_candidates_extracts_latin_and_quoted() -> None:
    kws = keyword_candidates("项目《云笔记》,基于 SpringBoot 开发,见 https://github.com/x/y")
    assert "SpringBoot" in kws
    assert "云笔记" in kws
    lowered = {k.lower() for k in kws}
    assert not lowered & {"github", "http", "https", "com", "www", "org"}


# ── 评审 P1 回归:词界等值匹配,防绑定名下无关仓被误判点名 ──


def test_repo_matching_requires_word_boundary() -> None:
    """关键词 'blog' 不得子串命中 'myblogengine'(D2:只深挖点名项目)。"""
    from official_agent.evaluation.attribution import _repo_matches

    row = {"name": "myblogengine", "description": "a blog engine"}
    assert _repo_matches(row, ["blog"]) is False
    assert _repo_matches(row, ["myblogengine"]) is True  # 全名等值
    assert _repo_matches({"name": "shop-mall"}, ["shop"]) is True  # 词元等值
    assert _repo_matches({"name": "shop-mall"}, ["shopmall"]) is True  # 去分隔符等值
    # 描述不再参与匹配(太松)
    assert _repo_matches({"name": "unrelated", "description": "blog platform"}, ["blog"]) is False


def test_contribution_regex_ignores_tech_stack_slash() -> None:
    assert detect_contribution_target("为 Vue/React 双栈开发") is None
    assert detect_contribution_target("给 kubernetes/kubernetes 贡献了调度器") == (
        "kubernetes",
        "kubernetes",
    )


@respx.mock
async def test_route_node_contribution_claim_bound_no_evidence_guides() -> None:
    """贡献声明 + 绑定但查无 commits/PR → claimed,不深挖只 guided(D4)。"""
    respx.get(f"{base}/repos/org/toolkit/commits").mock(_json([]))
    respx.get(f"{base}/search/issues").mock(_json({"items": []}))
    state = {
        "project_text": "给 org/toolkit 贡献了插件模块",
        "github_base": base,
        "candidate_login": "me",
    }
    result = await ig.route_node(state)
    assert result["route"] == "guided"
    assert result["attribution"]["level"] == "claimed"
    assert result["attribution"]["source"] == "contribution"
