"""把报告正文渲染成 Markdown。纯函数,零 IO。

模型写的只有各节的段落、总述与教训;标题、依据标注、指标表都由这里从快照生成,
所以表里的数不经过模型。渲染结果出边界前整体再过一遍脱敏兜底。
"""

from __future__ import annotations

from typing import Any

from official_agent.retrospective.catalog import STATUS_OK
from official_agent.retrospective.graph import (
    SECTION_ANALYST_FAILED,
    SECTION_NO_DATA,
    WRITER_FAILED,
)
from official_agent.retrospective.views import (
    display_value,
    entries_for,
    role_view,
)
from official_agent.security.pii import mask_pii

_LESSON_KIND_LABELS = {"lesson": "经验", "norm": "规矩", "calendar": "时间规律"}


def render_markdown(
    report: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    version: int,
    prior_lessons: list[dict[str, Any]] | None = None,
) -> str:
    """报告正文 + 它依据的快照 → Markdown 全文。

    prior_lessons 是往届留下、还没退役的组织记忆:列在报告末尾,请审阅的人对照本届的
    情况决定续期还是退役。
    """
    names = {entry["id"]: entry["name"] for entry in snapshot.get("catalog", [])}
    lines = [f"# 招新周期复盘报告 · 周期 {snapshot.get('cycle_id')}(第 {version} 版)", ""]
    baseline = snapshot.get("baseline")
    lines.append(
        f"> 依据的指标定义:{snapshot.get('schema')}"
        + (f";对比基线:周期 {baseline['cycle_id']}" if baseline else ";没有对比基线")
        + "。数字全部出自指标快照,文字由模型撰写、经程序核对;发布前需管理员审阅。"
    )
    if report.get("writer_status") == WRITER_FAILED:
        lines += ["", "> **未汇总**:撰稿人没有产出,以下各节直接列出分析员的发现。"]
    if report.get("summary"):
        lines += ["", "## 概览", "", report["summary"]["text"]]
    for section in report.get("sections", []):
        lines += ["", f"## {section['title']}", ""]
        lines += _section_body(section, snapshot, names)
    if report.get("lessons"):
        lines += ["", "## 候选教训", "", "逐条由管理员决定是否采纳;没有勾选的不会生效。", ""]
        for index, lesson in enumerate(report["lessons"], start=1):
            label = _LESSON_KIND_LABELS.get(lesson["kind"], lesson["kind"])
            lines.append(f"{index}. 〔{label}〕{lesson['content']}")
            lines.append(f"   依据:{_cited_names(lesson['metric_ids'], snapshot, names)}")
    if report.get("rubric_candidates"):
        lines += ["", "## 评分条款候选", "", "只是提议,要不要改评分说明由人决定。", ""]
        for index, candidate in enumerate(report["rubric_candidates"], start=1):
            lines.append(f"{index}. {candidate['text']}")
            lines.append(f"   依据:{_cited_names(candidate['metric_ids'], snapshot, names)}")
    if prior_lessons:
        lines += ["", "## 往届教训复核", ""]
        lines += ["这些是往届复盘留下、还没退役的经验。对照本届的情况,逐条决定续期还是退役。", ""]
        for index, lesson in enumerate(prior_lessons, start=1):
            label = _LESSON_KIND_LABELS.get(lesson["kind"], lesson["kind"])
            validity = "已过期" if lesson["expired"] else f"有效期至 {lesson['valid_until']}"
            lines.append(
                f"{index}. 〔{label}〕{lesson['content']}"
                f"(出自周期 {lesson['source_cycle']};{validity})"
            )
    if report.get("unverified"):
        lines += ["", "## 被程序改动过的地方", ""]
        lines += ["下面的内容没有进入报告,列出来供审阅时参考。", ""]
        for entry in report["unverified"]:
            lines.append(f"- [{entry['where']}] {entry['text']} —— {entry['reason']}")
    return mask_pii("\n".join(lines).strip() + "\n")


def _section_body(
    section: dict[str, Any], snapshot: dict[str, Any], names: dict[str, str]
) -> list[str]:
    view = role_view(snapshot, section["role"])
    lines: list[str] = []
    if section["status"] == SECTION_NO_DATA:
        reasons = ";".join(
            f"{domain}:{status.get('reason', '')}"
            for domain, status in sorted(view.domains.items())
            if status.get("status") != STATUS_OK
        )
        return [f"> 这一节的数据不可用。{reasons}"]
    if section["status"] == SECTION_ANALYST_FAILED:
        lines += ["> 这一节的分析员没有产出,下面只列指标,没有解读。", ""]
    for index, item in enumerate(section["items"], start=1):
        lines.append(f"{index}. {item['text']}")
        lines.append(f"   依据:{_cited_names(item['metric_ids'], snapshot, names)}")
    if section["items"]:
        lines.append("")
    lines += _metric_table(view.metrics, names)
    return lines


def _metric_label(metric: dict[str, Any], names: dict[str, str]) -> str:
    name = names.get(metric["metric"], metric["metric"])
    return f"{name} · {metric['key']}" if metric.get("key") else name


def _cited_names(
    metric_ids: list[str], snapshot: dict[str, Any], names: dict[str, str]
) -> str:
    """引用的指标 id → 人读的指标名(去重保序);引用目录项 id 时只写目录项名。"""
    labels: list[str] = []
    for metric_id in metric_ids:
        if metric_id in names:
            label = names[metric_id]
        else:
            entries = entries_for(snapshot.get("metrics", []), [metric_id])
            label = _metric_label(entries[0], names) if entries else metric_id
        if label not in labels:
            labels.append(label)
    return "、".join(labels)


def _metric_table(metrics: list[dict[str, Any]], names: dict[str, str]) -> list[str]:
    if not metrics:
        return []
    with_baseline = any("prev" in metric for metric in metrics)
    header = "| 指标 | 本期 | 样本量 |" + (" 基线 | 变化 |" if with_baseline else "")
    lines = [header, "|---|---|---|" + ("---|---|" if with_baseline else "")]
    for metric in metrics:
        sample = f"{metric['n']:,}" if metric.get("n") is not None else "—"
        row = f"| {_metric_label(metric, names)} | {display_value(metric)} | {sample} |"
        if with_baseline:
            row += f" {_baseline_cell(metric)} | {_change_cell(metric)} |"
        lines.append(row)
    return lines


def _baseline_cell(metric: dict[str, Any]) -> str:
    prev = metric.get("prev")
    if prev is None:
        return "—"
    return display_value({**metric, "value": prev, "suppressed": False})


def _change_cell(metric: dict[str, Any]) -> str:
    delta = metric.get("delta")
    if delta is None:
        return "—"
    if metric["unit"] == "ratio":
        return f"{delta * 100:+.2f} 个百分点"
    ratio = metric.get("delta_ratio")
    return f"{delta:+,}" + (f"({ratio * 100:+.2f}%)" if ratio is not None else "")
