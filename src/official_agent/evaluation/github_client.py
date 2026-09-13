"""GitHub 只读客户端(B3,#130;B-AG1 八工具,#149/D6)。

- 只读:仓库元数据 / README / 提交历史 / 文件树 + D6 八工具面(search_repos /
  list_user_repos / repo_meta / list_files / read_file / search_in_repo /
  read_commits+commit_detail / search_issues)
- base_url 可注入(respx 测试);GITHUB_TOKEN 经 settings 接线(D17,#149),
  code search 等端点无 token 必 401
- 私有/不可达统一抛 GitHubUnavailable(路由降级为通用引导题,#130)
- 单工具结果截断(D7):单文件/单 patch ≤8K 字符,截断留痕
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import quote

import httpx

_DEFAULT_BASE = "https://api.github.com"
_MAX_TEXT_CHARS = 8000  # D7:单文件/单 patch 截断上限


class GitHubUnavailable(RuntimeError):
    """仓库不可读(私有/不存在/限流/网络)——路由降级信号,不是错误。"""


def _truncate(text: str, *, max_chars: int = _MAX_TEXT_CHARS) -> tuple[str, bool]:
    """超限截断并留痕(探索 agent 需知道看到的是残篇)。"""
    if len(text) <= max_chars:
        return text, False
    return (
        text[:max_chars] + f"\n…[截断:共 {len(text)} 字符,超出 {max_chars} 上限]",
        True,
    )


def normalize_github_login(raw: str) -> str:
    """归一化 github 地址/登录名为登录名(对齐后端 GitHubAccountUtil 容忍度)。

    支持裸登录名 / https://github.com/x / github.com/x / git@github.com:x/y.git;
    统一小写(对齐后端 GitHubAccountUtil 规范形,owner==login 比较依赖此);
    解析不出 → 空串(语义:无绑定)。"""

    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    match = re.search(r"github\.com[/:]([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)", raw)
    if match:
        return match.group(1)
    if "/" not in raw and ":" not in raw and " " not in raw:
        return raw
    return ""


class GitHubClient:
    def __init__(self, base_url: str = _DEFAULT_BASE, token: str = "") -> None:
        self._base = base_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def repo(self, owner: str, repo: str) -> dict:
        """仓库元数据:size/stars/language/default_branch/pushed_at。"""
        data = await self._get_json(f"/repos/{owner}/{repo}")
        return data if isinstance(data, dict) else {}

    # ── D6 八工具面(#149) ─────────────────────────────────────

    async def repo_meta(self, owner: str, repo: str) -> dict:
        """仓基本面投影(D6 repo_meta):定位/fork 判定/默认分支/活跃度。

        fork 时 parent.full_name 一并带回(入口瀑布 D2:fork → 查 authored
        commits ⇒ trusted-contribution)。"""
        data = await self.repo(owner, repo)
        meta: dict = {
            "full_name": data.get("full_name", ""),
            "description": data.get("description") or "",
            "language": data.get("language") or "",
            "fork": bool(data.get("fork")),
            "default_branch": data.get("default_branch") or "main",
            "stargazers_count": int(data.get("stargazers_count") or 0),
            "pushed_at": data.get("pushed_at") or "",
            "html_url": data.get("html_url") or "",
        }
        parent = data.get("parent")
        if isinstance(parent, dict):
            meta["parent_full_name"] = parent.get("full_name", "")
        return meta

    async def list_files(
        self, owner: str, repo: str, *, branch: str | None = None, limit: int = 600
    ) -> tuple[list[str], bool]:
        """文件路径清单(D6 list_files)= tree_paths 语义:(paths, truncated)。"""
        return await self.tree_paths(owner, repo, branch=branch, limit=limit)

    async def read_file(
        self, owner: str, repo: str, path: str, *, branch: str | None = None
    ) -> dict:
        """读单文件(D6 read_file):文本 ≤8K 字符(D7),目录回子项清单。

        目录不是错误:返回 {"type":"dir", children:[…]},agent 应改用 list_files
        视角;404/私有/网络错统一 GitHubUnavailable(降级语义,#130)。"""
        params = {"ref": branch} if branch else None
        data = await self._get_json(
            f"/repos/{owner}/{repo}/contents/{quote(path.lstrip('/'))}", params=params
        )
        if isinstance(data, list):
            children = [str(item.get("path", "")) for item in data[:100] if isinstance(item, dict)]
            return {"type": "dir", "path": path, "children": children}
        if not isinstance(data, dict):
            raise GitHubUnavailable("contents 响应结构异常")
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            # 大文件(>1MB)contents API 返回 content=None+_links[git],不下拉 blob
            return {
                "type": "file",
                "path": path,
                "content": "",
                "truncated": False,
                "note": "content 缺失(超大文件/非 base64 编码),仅元数据可读",
                "size": int(data.get("size") or 0),
            }
        try:
            text = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError) as exc:
            raise GitHubUnavailable(f"文件内容解码失败:{exc}") from exc
        clipped, truncated = _truncate(text)
        return {
            "type": "file",
            "path": path,
            "content": clipped,
            "truncated": truncated,
            "size": int(data.get("size") or len(text)),
        }

    async def search_repos(self, query: str, *, per_page: int = 10) -> list[dict]:
        """按项目名/关键词搜仓(D6 search_repos):入口瀑布第 3 步(未绑定时)。"""
        data = await self._get_json(
            "/search/repositories",
            params={"q": query, "per_page": per_page, "sort": "best-match"},
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        return [self._repo_row(item) for item in items if isinstance(item, dict)]

    async def list_user_repos(self, username: str, *, per_page: int = 100) -> list[dict]:
        """列出用户名下仓(D6 list_user_repos):入口瀑布第 2 步(绑定时匹配 P)。

        type=owner 只看名下仓(fork 的 parent 不混入;D2:绑定账号下的其他
        仓不看,但 fork 仓本身仍是候选本人的深挖对象)。"""
        data = await self._get_json(
            f"/users/{username}/repos",
            params={"type": "owner", "sort": "updated", "per_page": per_page},
        )
        return [self._repo_row(item) for item in data if isinstance(item, dict)]

    async def search_in_repo(
        self, owner: str, repo: str, query: str, *, per_page: int = 20
    ) -> list[dict]:
        """仓内代码搜索(D6 search_in_repo);GitHub 要求带 token,匿名必 401。"""
        data = await self._get_json(
            "/search/code",
            params={"q": f"{query} repo:{owner}/{repo}", "per_page": per_page},
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        return [
            {
                "path": str(item.get("path", "")),
                "repository": str(
                    (item.get("repository") or {}).get("full_name", f"{owner}/{repo}")
                ),
            }
            for item in items
            if isinstance(item, dict)
        ]

    async def read_commits(
        self,
        owner: str,
        repo: str,
        *,
        author: str | None = None,
        per_page: int = 30,
    ) -> list[dict]:
        """提交历史(D6 read_commits):author 可选过滤(D3 fork 判定/贡献核对)。

        含 sha(供 commit_detail 下钻单 commit diff)。空仓返回 []。"""
        params: dict = {"per_page": per_page}
        if author:
            params["author"] = author
        data = await self._get_json(f"/repos/{owner}/{repo}/commits", params=params)
        return [
            {
                "sha": str(item.get("sha", "")),
                "message": item.get("commit", {}).get("message", ""),
                "date": item.get("commit", {}).get("author", {}).get("date", ""),
                "author_login": str((item.get("author") or {}).get("login", "")),
            }
            for item in data
            if isinstance(item, dict)
        ]

    async def commit_detail(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        max_patch_chars: int = _MAX_TEXT_CHARS,
        max_total_chars: int = 3 * _MAX_TEXT_CHARS,
    ) -> dict:
        """单 commit 详情(D6 read_commits 的 diff 下钻):files + patch(截断)。

        per-file ≤8K(D7),总预算 24K(=3×单文件上限,保证单文件截断后仍能
        展开);总预算按「装得下才装」贪心,破限文件只计数,结尾汇总留痕。"""
        data = await self._get_json(f"/repos/{owner}/{repo}/commits/{sha}")
        if not isinstance(data, dict):
            raise GitHubUnavailable("commit 响应结构异常")
        files: list[dict] = []
        total_chars = 0
        omitted = 0
        for item in data.get("files", []):
            if not isinstance(item, dict):
                continue
            patch = str(item.get("patch") or "")
            clipped, _ = _truncate(patch, max_chars=max_patch_chars)
            if total_chars + len(clipped) > max_total_chars:
                omitted += 1  # 装不下:只计数,不展开
                continue
            total_chars += len(clipped)
            files.append(
                {
                    "filename": item.get("filename", ""),
                    "status": item.get("status", ""),
                    "additions": int(item.get("additions") or 0),
                    "deletions": int(item.get("deletions") or 0),
                    "patch": clipped,
                }
            )
        if omitted:
            files.append(
                {"note": f"patch 总量超 {max_total_chars} 字符上限,{omitted} 个文件未展开"}
            )
        return {
            "sha": str(data.get("sha", "")),
            "message": data.get("commit", {}).get("message", ""),
            "author_login": str((data.get("author") or {}).get("login", "")),
            "files": files,
        }

    async def search_issues(
        self, owner: str, repo: str, query: str, *, is_pr: bool = True, per_page: int = 20
    ) -> list[dict]:
        """issue/PR 搜索(D6 search_issues):贡献声明核对(D4,is:pr 查贡献)。

        query 拼进 repo: 限定;author:xxx 由调用方写进 query。"""
        kind = "is:pr" if is_pr else "is:issue"
        data = await self._get_json(
            "/search/issues",
            params={
                "q": f"repo:{owner}/{repo} {kind} {query}".strip(),
                "per_page": per_page,
                "sort": "updated",
            },
        )
        items = data.get("items", []) if isinstance(data, dict) else []
        return [
            {
                "number": int(item.get("number") or 0),
                "title": str(item.get("title", "")),
                "state": str(item.get("state", "")),
                "is_pr": "pull_request" in item,
                "html_url": str(item.get("html_url", "")),
            }
            for item in items
            if isinstance(item, dict)
        ]

    @staticmethod
    def _repo_row(item: dict) -> dict:
        """搜索/列表结果的仓投影:工具面只回决策所需字段(TOOL-05)。"""
        owner = item.get("owner") or {}
        return {
            "full_name": str(item.get("full_name", "")),
            "name": str(item.get("name", "")),
            "owner_login": str(owner.get("login", "") if isinstance(owner, dict) else ""),
            "description": str(item.get("description") or ""),
            "fork": bool(item.get("fork")),
            "default_branch": str(item.get("default_branch") or "main"),
            "stargazers_count": int(item.get("stargazers_count") or 0),
            "pushed_at": str(item.get("pushed_at") or ""),
            "html_url": str(item.get("html_url") or ""),
        }

    # ── 既有 B3 面(#130,investigate v2 子图在用,#152 收口) ──

    async def readme(self, owner: str, repo: str) -> str:
        """README 原文(raw);没有 README → 空串(值得度扣分项,不报错)。"""
        url = f"{self._base}/repos/{owner}/{repo}/readme"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                url,
                headers={**self._headers(), "Accept": "application/vnd.github.raw+json"},
            )
        if resp.status_code == 404:
            return ""
        if resp.status_code != 200:
            raise GitHubUnavailable(f"README {resp.status_code}")
        return resp.text

    async def commits(self, owner: str, repo: str, *, per_page: int = 30) -> list[dict]:
        """最近提交(message/date);空仓返回 []。"""
        data = await self._get_json(f"/repos/{owner}/{repo}/commits", params={"per_page": per_page})
        return [
            {
                "message": item.get("commit", {}).get("message", ""),
                "date": item.get("commit", {}).get("author", {}).get("date", ""),
            }
            for item in data
        ]

    async def tree_paths(
        self, owner: str, repo: str, *, branch: str | None = None, limit: int = 600
    ) -> tuple[list[str], bool]:
        """文件路径清单;返回 (paths, truncated)。

        truncated=True 表示 GitHub 递归树被截断——路径白名单不完整,
        调用方(路径真实性校验)必须放宽,否则 full-app 仓会误判编造。
        branch 由调用方传入可省一次 repo() 调用(匿名配额 60/h,能省则省)。
        """
        if not branch:
            meta = await self.repo(owner, repo)
            branch = meta.get("default_branch") or "main"
        data = await self._get_json(
            f"/repos/{owner}/{repo}/git/trees/{branch}", params={"recursive": "1"}
        )
        if not isinstance(data, dict):
            return [], False
        paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]
        truncated = bool(data.get("truncated")) or len(paths) > limit
        return paths[:limit], truncated

    async def _get_json(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise GitHubUnavailable(f"GitHub 连接失败:{exc}") from exc
        if resp.status_code != 200:
            # 401/403(含限流)/404 与其余错误码,对路由都等价"此仓不可读"
            raise GitHubUnavailable(f"GitHub {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            # 200+非 JSON(代理劫持/门户页)在大陆网络并不罕见——同样按不可读降级
            raise GitHubUnavailable("GitHub 返回非 JSON 响应") from exc
