"""B-AG1 八工具面测试(#149/D6):respx fake 仓覆盖 + 截断 + 降级语义。

既有 repo/readme/commits/tree_paths 的 respx 用例在 test_investigation.py;
这里只测 D6 新增面。
"""

import base64
from urllib.parse import unquote

import pytest
import respx
from httpx import Response

from official_agent.evaluation.github_client import (
    GitHubClient,
    GitHubUnavailable,
    normalize_github_login,
)

base = "https://api.github.test"


def _client() -> GitHubClient:
    return GitHubClient(base_url=base, token="tok-1")


def _json(payload: dict | list) -> Response:
    return Response(200, json=payload)


# ── repo_meta / list_files ───────────────────────────────


@respx.mock
async def test_repo_meta_projects_fork_parent_and_branch() -> None:
    respx.get(f"{base}/repos/o/r").mock(
        _json(
            {
                "full_name": "o/r",
                "description": "desc",
                "language": "Python",
                "fork": True,
                "default_branch": "trunk",
                "stargazers_count": 3,
                "pushed_at": "2026-09-01T00:00:00Z",
                "html_url": "https://github.com/o/r",
                "parent": {"full_name": "upstream/r"},
            }
        )
    )
    meta = await _client().repo_meta("o", "r")
    assert meta["fork"] is True
    assert meta["parent_full_name"] == "upstream/r"
    assert meta["default_branch"] == "trunk"


@respx.mock
async def test_list_files_returns_paths_and_truncation_flag() -> None:
    respx.get(f"{base}/repos/o/r").mock(_json({"default_branch": "main"}))
    respx.get(f"{base}/repos/o/r/git/trees/main").mock(
        _json(
            {
                "tree": [
                    {"type": "blob", "path": "a.py"},
                    {"type": "tree", "path": "pkg"},
                    {"type": "blob", "path": "b.txt"},
                ],
                "truncated": True,
            }
        )
    )
    paths, truncated = await _client().list_files("o", "r")
    assert paths == ["a.py", "b.txt"]
    assert truncated is True


# ── read_file(截断 D7/目录/404 降级) ─────────────────────


def _content_resp(text: str) -> Response:
    return _json(
        {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(text.encode()).decode(),
            "size": len(text),
        }
    )


@respx.mock
async def test_read_file_truncates_over_8k_chars() -> None:
    long_text = "x" * 9000
    respx.get(f"{base}/repos/o/r/contents/src/a.py").mock(_content_resp(long_text))
    result = await _client().read_file("o", "r", "src/a.py")
    assert result["type"] == "file"
    assert result["truncated"] is True
    assert len(result["content"]) < 9000
    assert "截断" in result["content"]


@respx.mock
async def test_read_file_directory_returns_children_not_error() -> None:
    respx.get(f"{base}/repos/o/r/contents/src").mock(
        _json(
            [
                {"type": "file", "path": "src/a.py"},
                {"type": "file", "path": "src/b.py"},
            ]
        )
    )
    result = await _client().read_file("o", "r", "src")
    assert result["type"] == "dir"
    assert result["children"] == ["src/a.py", "src/b.py"]


@respx.mock
async def test_read_file_404_raises_unavailable() -> None:
    respx.get(f"{base}/repos/o/r/contents/ghost.py").mock(Response(404, json={}))
    with pytest.raises(GitHubUnavailable):
        await _client().read_file("o", "r", "ghost.py")


# ── search_repos / list_user_repos ───────────────────────


@respx.mock
async def test_search_repos_projects_rows() -> None:
    route = respx.get(f"{base}/search/repositories").mock(
        _json(
            {
                "total_count": 1,
                "items": [
                    {
                        "full_name": "o/cool",
                        "name": "cool",
                        "owner": {"login": "o"},
                        "description": "d",
                        "fork": False,
                        "default_branch": "main",
                        "stargazers_count": 9,
                    }
                ],
            }
        )
    )
    rows = await _client().search_repos("cool", per_page=5)
    assert rows[0]["full_name"] == "o/cool"
    assert rows[0]["owner_login"] == "o"
    assert "q=cool" in str(route.calls[0].request.url)


@respx.mock
async def test_list_user_repos_owner_only() -> None:
    route = respx.get(f"{base}/users/u/repos").mock(
        _json(
            [
                {"full_name": "u/a", "name": "a", "owner": {"login": "u"}, "fork": True},
            ]
        )
    )
    rows = await _client().list_user_repos("u")
    assert rows[0]["name"] == "a"
    assert "type=owner" in str(route.calls[0].request.url)


# ── search_in_repo(无 token 401 → 降级) ──────────────────


@respx.mock
async def test_search_in_repo_hits_code_search_with_token() -> None:
    route = respx.get(f"{base}/search/code").mock(
        _json({"total_count": 1, "items": [{"path": "a.py", "repository": {"full_name": "o/r"}}]})
    )
    rows = await _client().search_in_repo("o", "r", "TODO")
    assert rows == [{"path": "a.py", "repository": "o/r"}]
    req = route.calls[0].request
    assert "repo:o/r" in unquote(str(req.url))
    assert req.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_search_in_repo_anonymous_401_degrades() -> None:
    respx.get(f"{base}/search/code").mock(
        Response(401, json={"message": "Requires authentication"})
    )
    with pytest.raises(GitHubUnavailable):
        await GitHubClient(base_url=base).search_in_repo("o", "r", "x")


# ── read_commits(author 过滤)/ commit_detail(diff 截断) ──


@respx.mock
async def test_read_commits_filters_by_author_and_keeps_sha() -> None:
    route = respx.get(f"{base}/repos/o/r/commits").mock(
        _json(
            [
                {
                    "sha": "abc123",
                    "commit": {"message": "fix", "author": {"date": "2026-09-01"}},
                    "author": {"login": "me"},
                }
            ]
        )
    )
    rows = await _client().read_commits("o", "r", author="me")
    assert rows[0]["sha"] == "abc123"
    assert rows[0]["author_login"] == "me"
    assert "author=me" in str(route.calls[0].request.url)


@respx.mock
async def test_commit_detail_truncates_patch_total() -> None:
    respx.get(f"{base}/repos/o/r/commits/sha1").mock(
        _json(
            {
                "sha": "sha1",
                "commit": {"message": "big refactor"},
                "author": {"login": "me"},
                "files": [
                    {
                        "filename": "a.py",
                        "status": "modified",
                        "additions": 10,
                        "deletions": 2,
                        "patch": "p" * 6000,
                    },
                    {
                        "filename": "b.py",
                        "status": "modified",
                        "additions": 5,
                        "deletions": 1,
                        "patch": "q" * 6000,
                    },
                ],
            }
        )
    )
    detail = await _client().commit_detail("o", "r", "sha1")
    assert detail["author_login"] == "me"
    # 默认总预算 24K:6000+6000 双文件都在预算内,各自完整展开
    assert detail["files"][0]["patch"].startswith("p")
    assert detail["files"][1]["patch"].startswith("q")
    assert not [f for f in detail["files"] if "note" in f]


# ── search_issues(is:pr 查贡献) ──────────────────────────


@respx.mock
async def test_search_issues_builds_pr_query() -> None:
    route = respx.get(f"{base}/search/issues").mock(
        _json(
            {
                "items": [
                    {
                        "number": 7,
                        "title": "add feature",
                        "state": "closed",
                        "pull_request": {"url": "x"},
                        "html_url": "https://github.com/o/r/pull/7",
                    }
                ]
            }
        )
    )
    rows = await _client().search_issues("o", "r", "author:me feat")
    assert rows[0]["is_pr"] is True
    assert rows[0]["number"] == 7
    decoded = unquote(str(route.calls[0].request.url))
    assert "is:pr" in decoded
    assert "author:me" in decoded


# ── 统一降级语义(非 200/网络错/非 JSON) ──────────────────


@respx.mock
async def test_non_json_200_raises_unavailable() -> None:
    respx.get(f"{base}/search/repositories").mock(Response(200, text="<html>portal</html>"))
    with pytest.raises(GitHubUnavailable, match="非 JSON"):
        await _client().search_repos("x")


@respx.mock
async def test_rate_limit_403_raises_unavailable() -> None:
    respx.get(f"{base}/repos/o/r").mock(Response(403, json={"message": "rate limit"}))
    with pytest.raises(GitHubUnavailable):
        await _client().repo_meta("o", "r")


# ── 登录名归一化(D17 档案 github 字段) ───────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://github.com/zewang", "zewang"),
        ("github.com/zewang/", "zewang"),
        ("git@github.com:zewang/repo.git", "zewang"),
        ("zewang", "zewang"),
        ("", ""),
        ("https://gitlab.com/x", ""),
    ],
)
def test_normalize_github_login(raw: str, want: str) -> None:
    assert normalize_github_login(raw) == want


# ── 评审 P2 补测:超大文件/无 parent/单文件超预算/大小写归一 ──


@respx.mock
async def test_read_file_oversized_returns_metadata_only() -> None:
    respx.get(f"{base}/repos/o/r/contents/huge.bin").mock(
        _json({"type": "file", "encoding": "none", "content": None, "size": 3_000_000})
    )
    result = await _client().read_file("o", "r", "huge.bin")
    assert result["content"] == ""
    assert result["size"] == 3_000_000
    assert "content 缺失" in result["note"]


@respx.mock
async def test_repo_meta_without_parent_has_no_parent_key() -> None:
    respx.get(f"{base}/repos/o/plain").mock(_json({"full_name": "o/plain", "fork": False}))
    meta = await _client().repo_meta("o", "plain")
    assert meta["fork"] is False
    assert "parent_full_name" not in meta


@respx.mock
async def test_commit_detail_truncated_file_eats_budget() -> None:
    respx.get(f"{base}/repos/o/r/commits/big").mock(
        _json(
            {
                "sha": "big",
                "commit": {"message": "one huge file"},
                "files": [
                    {"filename": "a.py", "additions": 1, "deletions": 0, "patch": "z" * 9000},
                    {"filename": "b.py", "additions": 1, "deletions": 0, "patch": "q"},
                ],
            }
        )
    )
    detail = await _client().commit_detail("o", "r", "big")
    # 单文件截到 8K+留痕;总预算 24K(3×单文件上限)→ 小文件仍展开
    assert "截断" in detail["files"][0]["patch"]
    assert detail["files"][1]["patch"] == "q"
    assert not [f for f in detail["files"] if "note" in f]


@respx.mock
async def test_commit_detail_budget_overrun_counts_omitted() -> None:
    respx.get(f"{base}/repos/o/r/commits/wide").mock(
        _json(
            {
                "sha": "wide",
                "commit": {"message": "many files"},
                "files": [
                    {"filename": "a.py", "patch": "p" * 6000},
                    {"filename": "b.py", "patch": "q" * 6000},
                    {"filename": "c.py", "patch": "r" * 100},
                ],
            }
        )
    )
    detail = await _client().commit_detail("o", "r", "wide", max_total_chars=8000)
    # 装得下才装:a 进(6000),b 装不下(12000>8000)省略,c 仍可进(6100≤8000)
    assert [f["filename"] for f in detail["files"] if "filename" in f] == ["a.py", "c.py"]
    notes = [f for f in detail["files"] if "note" in f]
    assert len(notes) == 1 and "1 个文件未展开" in notes[0]["note"]


def test_normalize_github_login_lowercases() -> None:
    assert normalize_github_login("https://github.com/Zewang") == "zewang"
    assert normalize_github_login("Zewang") == "zewang"
