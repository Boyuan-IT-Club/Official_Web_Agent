"""调查子图路由与值得度(B3,#130,含 #129 合并裁定):纯函数决策层。

- 路由(#130):短文本+有仓→深挖;短文本+无仓→skip;
  有项目文本但无仓/仓不可读→通用引导题(不 skip 项目维)
- 值得度(#130):README 体量/提交数/文件面结构 → none|low|high → 定题数
- 全部零 IO(IO 在 github_client/LLM 节点),决策可单测
"""

from __future__ import annotations

import re
from typing import Literal

# 项目维字段键的语义匹配(周期配置驱动,键名不稳定,按 label/键名猜)
_PROJECT_HINTS = ("project", "项目")
# 仓库 URL:github.com/owner/repo。左断言防 "mygithub.com" 伪站;
# repo 段含 . 但捕获后剥尾随标点("repo." 句点收尾是常见书写)
_REPO_URL = re.compile(
    r"(?<![A-Za-z0-9-])github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)

# 有实质内容的最短长度(#130「短文本但说了做了什么」)
_MIN_SUBSTANTIVE = 24

# 值得度信号
_SIGNAL_DIRS = ("src/", "app/", "tests/", "test/", "docs/", "server/", "client/")


def _strip_repo(owner: str, repo: str) -> tuple[str, str] | None:
    """清洗单个 owner/repo:剥 .git 与尾随标点;空段返回 None。"""
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip(".,;:)!?}]")  # 尾随标点是书写标点,不属于仓名
    if not owner or not repo:
        return None
    return owner, repo


def extract_repos(text: str) -> list[tuple[str, str]]:
    """提取文本里**全部** github.com/owner/repo,保序去重。

    M-1(遗留①):旧 extract_repo 只返回首个匹配,第二个仓(如候选简历里的
    myloop-meta)被静默丢弃。调用方据此逐仓深挖。
    """
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for m in _REPO_URL.finditer(text or ""):
        cleaned = _strip_repo(m.group(1), m.group(2))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def extract_repo(text: str) -> tuple[str, str] | None:
    """从项目文本提取首个 (owner, repo);无 GitHub 链接返回 None。"""
    repos = extract_repos(text)
    return repos[0] if repos else None


def route_project(
    project_text: str, repo_readable: bool | None
) -> Literal["deep_dive", "skip", "guided"]:
    """路由判据(#130)。repo_readable:None=无仓位置;True/False=有仓且探测结果。

    - 有仓 + 可读 → deep_dive
    - 无仓 + 无实质内容 → skip(此维不浪费题)
    - 其余(有仓不可读 / 无仓但有实质内容)→ guided
    """
    substantive = len((project_text or "").strip()) >= _MIN_SUBSTANTIVE
    has_repo = repo_readable is not None
    if has_repo and repo_readable:
        return "deep_dive"
    if not has_repo and not substantive:
        return "skip"
    return "guided"
