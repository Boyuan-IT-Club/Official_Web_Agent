"""分析员的指标视图:快照里哪些指标给哪位分析员看,一个值怎么写成人读的文字。纯函数。

三位分析员各看各的域、互相看不到对方的发现(docs/proposals/cycle-retrospective.md
D2)——避免一个先入为主的解释把三份分析带偏。视图在这里按角色裁好,分析员拿到的
prompt 里根本没有别的域的数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from official_agent.retrospective.catalog import (
    DOMAIN_AGREEMENT,
    DOMAIN_CONVERSATION,
    DOMAIN_FUNNEL,
    DOMAIN_KB,
    DOMAIN_QBANK,
    DOMAIN_SCREENING,
    STATUS_OK,
)
from official_agent.retrospective.guard import (
    SIDE_HIGH,
    SIDE_LOW,
    UNIT_MS,
    UNIT_RATIO,
    UNIT_TOKENS,
)

ROLE_DATA = "data"
ROLE_QA = "qa"
ROLE_EVALUATION = "evaluation"


@dataclass(frozen=True)
class RoleSpec:
    """一位分析员:报告里对应的章节、prompt 文件、看得到的指标范围。

    extra_metrics 是域之外额外可见的目录项——成本域按「谁的 token」分给两位。
    """

    role: str
    title: str
    prompt_file: str
    domains: frozenset[str]
    extra_metrics: frozenset[str] = frozenset()


ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(ROLE_DATA, "漏斗与部门", "retrospective/data.md", frozenset({DOMAIN_FUNNEL})),
    RoleSpec(
        ROLE_QA,
        "对话与知识库",
        "retrospective/qa.md",
        frozenset({DOMAIN_CONVERSATION, DOMAIN_KB}),
        frozenset({"cost.conversation_tokens"}),
    ),
    RoleSpec(
        ROLE_EVALUATION,
        "初筛与出题",
        "retrospective/evaluation.md",
        frozenset({DOMAIN_SCREENING, DOMAIN_AGREEMENT, DOMAIN_QBANK}),
        frozenset(
            {
                "cost.evaluation_tokens",
                "cost.evaluation_tokens_per_resume",
                "cost.scoring_tokens",
            }
        ),
    ),
)

ROLES: tuple[str, ...] = tuple(spec.role for spec in ROLE_SPECS)
_SPEC_BY_ROLE = {spec.role: spec for spec in ROLE_SPECS}


def role_spec(role: str) -> RoleSpec:
    return _SPEC_BY_ROLE[role]


@dataclass(frozen=True)
class RoleView:
    """快照按一位分析员裁出来的部分。"""

    role: str
    #: 该角色可见的目录项(含暂缺的)
    catalog: list[dict[str, Any]]
    #: 该角色可见的指标值(快照 metrics 的子集;带基线时每项有 prev/delta/delta_ratio)
    metrics: list[dict[str, Any]]
    #: 该角色涉及的域 → 采集状态
    domains: dict[str, dict[str, Any]]
    #: 目录里有、这次没取到值的指标
    absent: list[dict[str, Any]]

    def citable_ids(self) -> frozenset[str]:
        """发现里可以引用的 id:每个值的 id,加上目录项 id(引用整张分布,或指出数据缺口)。"""
        return frozenset(
            [m["id"] for m in self.metrics] + [entry["id"] for entry in self.catalog]
        )


def role_view(snapshot: dict[str, Any], role: str) -> RoleView:
    """从快照正文里裁出一位分析员的视图。"""
    spec = role_spec(role)
    catalog = [
        entry
        for entry in snapshot.get("catalog", [])
        if entry["domain"] in spec.domains or entry["id"] in spec.extra_metrics
    ]
    visible = {entry["id"] for entry in catalog}
    return RoleView(
        role=role,
        catalog=catalog,
        metrics=[m for m in snapshot.get("metrics", []) if m["metric"] in visible],
        domains={
            domain: status
            for domain, status in (snapshot.get("domains") or {}).items()
            if domain in spec.domains
        },
        absent=[a for a in snapshot.get("absent", []) if a["id"] in visible],
    )


def entries_for(metrics: list[dict[str, Any]], cited: list[str]) -> list[dict[str, Any]]:
    """引用的 id → 对应的指标值条目。引用目录项 id 等于引用它名下的每一格。"""
    wanted = set(cited)
    return [m for m in metrics if m["id"] in wanted or m["metric"] in wanted]


def display_value(metric: dict[str, Any]) -> str:
    """一个指标值写成人读的文字;被抑制的值写成范围,不写成 0。"""
    if metric.get("suppressed"):
        side, n = metric.get("side"), metric.get("n")
        if side == SIDE_LOW:
            return f"不足 5(共 {n:,})" if metric["unit"] == UNIT_RATIO and n else "不足 5"
        if side == SIDE_HIGH:
            return f"接近全部(与 {n:,} 相差不足 5)"
        return "样本不足"
    return _format_number(metric.get("value"), metric["unit"])


def _format_number(value: Any, unit: str) -> str:
    """大数带千分位:便于读,也免得七八位的 token 合计被出口的脱敏规则当成 QQ 号。"""
    if value is None:
        return "无"
    if unit == UNIT_RATIO:
        # 两位小数的百分比:与快照里比率保留 4 位小数的精度一致
        return f"{value * 100:.2f}%"
    if unit == UNIT_MS:
        return f"{value:,} 毫秒"
    if unit == UNIT_TOKENS:
        return f"{value:,} token"
    return f"{value:,}"


def _display_change(metric: dict[str, Any]) -> str:
    """相对基线的变化;没带基线或任一侧没有值时为空串。"""
    prev, delta = metric.get("prev"), metric.get("delta")
    if prev is None or delta is None:
        return ""
    unit = metric["unit"]
    if unit == UNIT_RATIO:
        return f";基线 {_format_number(prev, unit)},变化 {delta * 100:+.2f} 个百分点"
    change = f";基线 {_format_number(prev, unit)},变化 {delta:+,}"
    ratio = metric.get("delta_ratio")
    return change + (f"({ratio * 100:+.2f}%)" if ratio is not None else "")


def describe_view(view: RoleView) -> str:
    """视图 → 给模型看的文本:按目录项分组,每个值一行,带口径与缺口说明。"""
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for metric in view.metrics:
        by_metric.setdefault(metric["metric"], []).append(metric)
    absent = {a["id"]: a["reason"] for a in view.absent}
    lines: list[str] = []
    for domain, status in sorted(view.domains.items()):
        if status.get("status") != STATUS_OK:
            lines.append(f"[域 {domain} 没有数据:{status.get('reason', '')}]")
    for entry in view.catalog:
        lines.append(f"## {entry['id']} · {entry['name']}")
        lines.append(f"口径:{entry['definition']}")
        if entry.get("status") != STATUS_OK:
            lines.append(f"(暂缺:{entry.get('reason', '')})")
            continue
        if entry["id"] in absent:
            lines.append(f"(这次没有取到:{absent[entry['id']]})")
            continue
        values = by_metric.get(entry["id"], [])
        if not values:
            lines.append("(没有数据)")
        for metric in values:
            sample = f";样本量 {metric['n']:,}" if metric.get("n") is not None else ""
            lines.append(
                f"- {metric['id']} = {display_value(metric)}{sample}{_display_change(metric)}"
            )
    return "\n".join(lines)
