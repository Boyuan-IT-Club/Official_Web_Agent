"""回复出口守卫的判定在对话日志里怎么存:一轮可以有多个守卫,共用一列文本。

格式是 `守卫名=判定`,多个守卫用分号连接,例如
`fabrication_empty_tools=clean;kb_citation=unverified:技术部介绍`。没跑的守卫不出现;
一个守卫都没跑时整列为空(None)。

选文本而不是 jsonb:这一列本来就是文本,值很短,查询方用 LIKE 就能筛
(`guard_verdict LIKE '%kb_citation=unverified:%'`),不需要迁移列类型。

只有编造守卫一个写入方的时期,这一列存的是不带守卫名的裸判定(`clean` /
`triggered:查询结果`);parse_verdicts 把这种值读成编造守卫的判定。
"""

from __future__ import annotations

from collections.abc import Mapping

_SEPARATOR = ";"
_ASSIGN = "="
#: 不带守卫名的旧值归到这个守卫名下(当时唯一的写入方)
LEGACY_GUARD_NAME = "fabrication_empty_tools"
#: 单个判定的长度上限:判定里可能带着命中的片段或条目标题,不让一列被撑得很长
_MAX_VERDICT_CHARS = 120


def _clean(verdict: str) -> str:
    """判定里不能有分隔符与换行:否则读的时候会被切成两段。"""
    flat = " ".join(verdict.replace(_SEPARATOR, ",").split())
    return flat[:_MAX_VERDICT_CHARS]


def format_verdicts(verdicts: Mapping[str, str]) -> str | None:
    """{守卫名: 判定} → 落库的文本;没有任何守卫的判定时返回 None。顺序按传入的顺序。"""
    parts = [f"{name}{_ASSIGN}{_clean(verdict)}" for name, verdict in verdicts.items()]
    return _SEPARATOR.join(parts) if parts else None


def parse_verdicts(value: str | None) -> dict[str, str]:
    """落库的文本 → {守卫名: 判定}。空值返回空字典;不带守卫名的旧值归到编造守卫名下。"""
    if not value:
        return {}
    verdicts: dict[str, str] = {}
    for part in value.split(_SEPARATOR):
        name, assign, verdict = part.partition(_ASSIGN)
        if assign and name.strip():
            verdicts[name.strip()] = verdict
        elif part.strip():
            verdicts[LEGACY_GUARD_NAME] = part.strip()
    return verdicts
