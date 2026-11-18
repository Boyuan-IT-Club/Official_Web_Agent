"""两组 eval 报告的逐条对照:哪些用例「原错新对」「原对新错」,分母是多少。

输入是 run_evals.py --json 的产物。每一侧可以给多份报告(同一条件重复跑几轮):
逐条统计通过次数,按多数定这一侧的结论;平票算不稳定,单列出来,不归进任何一类。
只比两侧都有的用例;只在一侧出现的用例单独报数,不混进分母。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SuiteComparison:
    """一个 suite 在两组运行之间的对照。各列表里是用例 id 与两侧的通过次数。"""

    name: str
    runs_before: int
    runs_after: int
    fixed: list[str] = field(default_factory=list)
    broke: list[str] = field(default_factory=list)
    still_pass: list[str] = field(default_factory=list)
    still_fail: list[str] = field(default_factory=list)
    unstable: list[str] = field(default_factory=list)
    only_one_side: list[str] = field(default_factory=list)
    tallies: dict[str, tuple[int, int]] = field(default_factory=dict)
    context_before: dict[str, Any] = field(default_factory=dict)
    context_after: dict[str, Any] = field(default_factory=dict)
    observations_before: dict[str, float] = field(default_factory=dict)
    observations_after: dict[str, float] = field(default_factory=dict)

    @property
    def compared(self) -> int:
        return (
            len(self.fixed)
            + len(self.broke)
            + len(self.still_pass)
            + len(self.still_fail)
            + len(self.unstable)
        )


def _suites(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in report.get("suites") or []}


def _verdict(passes: int, runs: int) -> bool | None:
    """多数通过为真、多数失败为假、平票为 None。"""
    if passes * 2 == runs:
        return None
    return passes * 2 > runs


def compare_reports(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[SuiteComparison]:
    """before / after 各是一组报告(同一条件的重复运行)。返回两侧都有的 suite 的对照。"""
    if not before or not after:
        raise ValueError("两侧都至少要一份报告")
    names = set.intersection(*(set(_suites(r)) for r in [*before, *after]))
    out: list[SuiteComparison] = []
    for name in sorted(names):
        side_b = [_suites(r)[name] for r in before]
        side_a = [_suites(r)[name] for r in after]
        cmp = SuiteComparison(
            name=name,
            runs_before=len(side_b),
            runs_after=len(side_a),
            context_before=side_b[0].get("context") or {},
            context_after=side_a[0].get("context") or {},
            observations_before=side_b[0].get("observations") or {},
            observations_after=side_a[0].get("observations") or {},
        )
        passes_b = _tally(side_b)
        passes_a = _tally(side_a)
        for case_id in sorted(set(passes_b) | set(passes_a)):
            if case_id not in passes_b or case_id not in passes_a:
                cmp.only_one_side.append(case_id)
                continue
            cmp.tallies[case_id] = (passes_b[case_id], passes_a[case_id])
            was = _verdict(passes_b[case_id], len(side_b))
            now = _verdict(passes_a[case_id], len(side_a))
            if was is None or now is None:
                cmp.unstable.append(case_id)
            elif not was and now:
                cmp.fixed.append(case_id)
            elif was and not now:
                cmp.broke.append(case_id)
            elif now:
                cmp.still_pass.append(case_id)
            else:
                cmp.still_fail.append(case_id)
        out.append(cmp)
    return out


def _tally(runs: list[dict[str, Any]]) -> dict[str, int]:
    """用例 id → 通过次数;某轮缺了这条用例,该 id 就不计入(只比每轮都跑到的)。"""
    seen: list[dict[str, bool]] = [
        {c["id"]: bool(c["passed"]) for c in run.get("cases") or []} for run in runs
    ]
    common = set.intersection(*(set(s) for s in seen)) if seen else set()
    return {cid: sum(s[cid] for s in seen) for cid in common}


def render(comparisons: list[SuiteComparison]) -> str:
    """对照结果 → 可直接贴进报告的 Markdown。"""
    lines: list[str] = []
    for c in comparisons:
        lines.append(f"## {c.name}")
        lines.append("")
        lines.append(f"- 运行条件(前):{json.dumps(c.context_before, ensure_ascii=False)}")
        lines.append(f"- 运行条件(后):{json.dumps(c.context_after, ensure_ascii=False)}")
        lines.append(f"- 每侧轮数:前 {c.runs_before},后 {c.runs_after};比较用例 {c.compared} 条")
        lines.append("")
        lines.append("| 类别 | 条数 | 用例(前通过次数 → 后通过次数) |")
        lines.append("|---|---|---|")
        for title, ids in (
            ("原错新对", c.fixed),
            ("原对新错", c.broke),
            ("一直通过", c.still_pass),
            ("一直失败", c.still_fail),
            ("结果不稳定(平票)", c.unstable),
        ):
            shown = ", ".join(f"{i}({c.tallies[i][0]}→{c.tallies[i][1]})" for i in ids)
            lines.append(f"| {title} | {len(ids)}/{c.compared} | {shown} |")
        if c.only_one_side:
            lines.append(f"\n只在一侧出现、未计入:{', '.join(c.only_one_side)}")
        keys = sorted(set(c.observations_before) | set(c.observations_after))
        if keys:
            lines.append("\n| 观测量 | 前 | 后 |\n|---|---|---|")
            for key in keys:
                before = c.observations_before.get(key, "—")
                after = c.observations_after.get(key, "—")
                lines.append(f"| {key} | {before} | {after} |")
        lines.append("")
    return "\n".join(lines)


def load_reports(paths: list[Path]) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]
