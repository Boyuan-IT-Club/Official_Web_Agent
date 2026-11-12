"""引用核验:回复里用书名号引的知识库条目,是不是这一轮真的检索到的。

只观测,不改写回复:判定随对话日志落库(guard_verdict 里的 kb_citation 一项),用来
回答「模型有没有引用它没查到的条目」。确定性规则,不调模型,不增加回合延迟。

三种判定:
- clean:回复里引用的条目都在本轮检索到的条目里;
- unverified:<标题>:本轮检索过知识库,但引用的这个标题不在检索到的条目里;
- unsearched:<标题>:回复里有书名号引用,而本轮根本没有检索知识库。
回复里没有书名号引用时不产生判定(守卫不适用)。

已知的误报来源,所以现在只观测:引用上一轮检索到的条目(本轮没再查)、回复转述了
条目正文里提到的文件名、书名号用在与知识库无关的地方(课程名、书名)。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

GUARD_NAME = "kb_citation"

VERDICT_CLEAN = "clean"
VERDICT_UNVERIFIED = "unverified"
VERDICT_UNSEARCHED = "unsearched"

#: 书名号里的内容;不跨行,过长的多半不是条目标题
_CITED_RE = re.compile(r"《([^《》\n]{1,60})》")


def _normalize(title: str) -> str:
    return "".join(title.split()).lower()


def _matches(cited: str, retrieved: str) -> bool:
    """引用的标题与检索到的标题算不算同一条:相同,或一个包含另一个。

    模型常把标题截短或补上版本(「《技术部介绍》」对「技术部介绍(2026)」),包含关系
    放过这种写法;单个字的包含不算。
    """
    a, b = _normalize(cited), _normalize(retrieved)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 2 and (a in b or b in a)


def check_citations(
    reply: str, retrieved_titles: Sequence[str], *, searched: bool
) -> str | None:
    """核对回复里的书名号引用,返回判定;回复里没有引用返回 None。

    retrieved_titles 是本轮知识库检索返回的条目标题;searched 表示本轮有没有调用过
    知识库检索(调用了但没命中时 retrieved_titles 为空、searched 为真)。
    """
    cited = list(dict.fromkeys(m.group(1).strip() for m in _CITED_RE.finditer(reply or "")))
    cited = [title for title in cited if title]
    if not cited:
        return None
    if not searched:
        return f"{VERDICT_UNSEARCHED}:{cited[0]}"
    for title in cited:
        if not any(_matches(title, known) for known in retrieved_titles):
            return f"{VERDICT_UNVERIFIED}:{title}"
    return VERDICT_CLEAN
