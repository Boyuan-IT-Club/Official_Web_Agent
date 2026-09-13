"""eval 引擎:suite 发现 → executor 分发 → 汇总报告 → 退出码(#148)。

设计:
- suite 文件用顶层 ``runner:`` 字段声明执行器 kind;未声明时按结构嗅探,
  兼容存量两份文件(probes+defaults → kb_probes;expected_tools 列表 →
  tool_selection)。新探针落新 kind + 新 executor,嗅探不再扩张。
- executor 是 ``(path, *, distribution) → SuiteResult`` 的异步函数;依赖
  (检索函数/agent 构造器)经模块级默认参数注入,测试换 fake 不碰产品码。
- 基线:metrics 全部按「越高越好」语义;``--baseline`` 对比时任何指标低于
  基线即 REGRESSION → FAIL。
- 退出码:0 全部通过 | 1 有 FAIL | 2 全部 SKIP(环境未配置)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


#: 单条用例结果(用例断言型 suite 的最小粒度)
@dataclass(slots=True)
class CaseResult:
    id: str
    passed: bool
    detail: str = ""
    skipped: bool = False  # 显式 SKIP(待依赖落地):不计 pass_rate 分母


@dataclass(slots=True)
class SuiteResult:
    """一个 suite 文件的执行结果。metrics 语义统一为「越高越好」。"""

    name: str
    kind: str
    source: str  # 相对 evals/ 的路径,如 cases/tool_selection.yaml
    status: str  # PASS | FAIL | SKIP
    cases: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        scored = [c for c in self.cases if not c.skipped]
        if not scored:
            return 1.0 if self.status == "PASS" else 0.0
        return sum(1 for c in scored if c.passed) / len(scored)


#: executor 签名:path → (distribution 透传,分布模式只看不设门) → 结果
Executor = Callable[..., Awaitable[SuiteResult]]
#: 环境自检:返回缺失项描述,None = 可跑
EnvBlocker = Callable[[], str | None]


@dataclass(slots=True)
class SuiteSpec:
    run: Executor
    env_blocker: EnvBlocker


@dataclass(slots=True)
class SuiteRef:
    """发现的 suite 文件:名字、kind、路径与所在面(cases/datasets)。"""

    name: str
    kind: str
    path: Path
    area: str  # cases | datasets


def _sniff_kind(data: Any) -> str:
    """无 ``runner:`` 字段的历史格式按结构嗅探;新 suite 一律显式声明。"""
    if isinstance(data, dict):
        if "probes" in data:
            return "kb_probes"
        if "cases" in data:
            return "tool_selection"
    if (
        isinstance(data, list)
        and data
        and isinstance(data[0], dict)
        and "expected_tools" in data[0]
    ):
        return "tool_selection"  # 早期裸列表格式
    raise ValueError("suite 文件缺少顶层 runner: 字段且结构无法识别(新 suite 必须显式声明 runner:)")


def discover_suites(evals_dir: Path) -> list[SuiteRef]:
    """扫 cases/ 与 datasets/ 下全部 yaml,解析 kind(显式声明优先)。"""
    refs: list[SuiteRef] = []
    for area in ("cases", "datasets"):
        for path in sorted((evals_dir / area).glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            kind = ""
            if isinstance(data, dict) and data.get("runner"):
                kind = str(data["runner"])
            else:
                kind = _sniff_kind(data)
            refs.append(SuiteRef(name=path.stem, kind=kind, path=path, area=area))
    return refs


def default_registry() -> dict[str, SuiteSpec]:
    """kind → (executor, 环境自检)。依赖注入在各 executor 模块的默认参数里。"""
    from official_agent.evals import (
        injection_probes,
        kb_probes,
        qbank_judge,
        qbank_probes,
        tool_selection,
    )

    return {
        "kb_probes": SuiteSpec(run=kb_probes.run_suite, env_blocker=kb_probes.env_blocker),
        "tool_selection": SuiteSpec(
            run=tool_selection.run_suite, env_blocker=tool_selection.env_blocker
        ),
        "injection_probes": SuiteSpec(
            run=injection_probes.run_suite, env_blocker=injection_probes.env_blocker
        ),
        "qbank_probes": SuiteSpec(run=qbank_probes.run_suite, env_blocker=qbank_probes.env_blocker),
        "qbank_judge": SuiteSpec(run=qbank_judge.run_suite, env_blocker=qbank_judge.env_blocker),
    }


def exit_code(results: list[SuiteResult]) -> int:
    """0 全过 | 1 有 FAIL | 2 全 SKIP(环境未配置,门禁不可静默变绿)。"""
    if any(r.status == "FAIL" for r in results):
        return 1
    if results and all(r.status == "SKIP" for r in results):
        return 2
    return 0


def _apply_baseline(result: SuiteResult, baseline_entry: dict[str, Any]) -> None:
    """任何指标低于基线 → REGRESSION,套件转 FAIL(基线即门,OBS-07)。"""
    recorded = baseline_entry.get("metrics", {}) if isinstance(baseline_entry, dict) else {}
    for key, base_value in recorded.items():
        cur = result.metrics.get(key)
        if cur is None:
            # 静默跳过会让改名的指标逃过门禁,至少留痕
            result.notes.append(f"基线指标 {key} 本次未产出(可能已改名),跳过对比")
            continue
        if cur < float(base_value):
            result.notes.append(f"REGRESSION: {key}={cur:.3f} < 基线 {float(base_value):.3f}")
            result.status = "FAIL"


async def run_suites(
    evals_dir: Path,
    *,
    only: str | None = None,
    suite: str | None = None,
    registry: dict[str, SuiteSpec] | None = None,
    distribution: bool = False,
    baseline: dict[str, Any] | None = None,
) -> list[SuiteResult]:
    """发现并执行 suite。any 坏文件(无法解析/未知 kind)直接抛,不做静默跳过。"""
    reg = registry if registry is not None else default_registry()
    refs = discover_suites(evals_dir)
    if only:
        refs = [r for r in refs if r.area == only]
    if suite:
        refs = [r for r in refs if r.name == suite]
    if not refs:
        raise ValueError(f"evals/{only or ''} 下没有匹配的 suite 文件")

    results: list[SuiteResult] = []
    for ref in refs:
        spec = reg.get(ref.kind)
        if spec is None:
            raise ValueError(f"未知 runner kind: {ref.kind}({ref.path.name})")
        blocker = spec.env_blocker()
        if blocker:
            results.append(
                SuiteResult(
                    name=ref.name,
                    kind=ref.kind,
                    source=f"{ref.area}/{ref.path.name}",
                    status="SKIP",
                    notes=[f"环境未配置:{blocker}"],
                )
            )
            continue
        result = await spec.run(ref.path, distribution=distribution)
        result.source = f"{ref.area}/{ref.path.name}"
        if result.cases:
            result.metrics["pass_rate"] = result.pass_rate
        if baseline and ref.name in baseline:
            _apply_baseline(result, baseline[ref.name])
        results.append(result)
    return results


def print_report(results: list[SuiteResult]) -> None:
    """人类可读报告:一行一 suite + 失败明细 + 备注。"""
    print("== eval report ==")
    for r in results:
        metrics = " ".join(f"{k}={v:.3f}" for k, v in r.metrics.items())
        line = f"[{r.status}] {r.name} ({r.kind}, {r.source})"
        if r.cases:
            line += f"  {len(r.cases)} cases"
        if metrics:
            line += f"  {metrics}"
        print(line)
        for c in r.cases:
            if not c.passed:
                print(f"  FAIL {c.id}: {c.detail}")
        for note in r.notes:
            print(f"  NOTE {note}")
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIP")
    print(f"== {len(results)} suites: {failed} failed, {skipped} skipped ==")


def dump_json(results: list[SuiteResult]) -> dict[str, Any]:
    """机器可读结构(--json 落盘 / 基线生成的数据源)。"""
    suites = []
    for r in results:
        suites.append(
            {
                "name": r.name,
                "kind": r.kind,
                "source": r.source,
                "status": r.status,
                "pass_rate": r.pass_rate,
                "metrics": r.metrics,
                "cases": [{"id": c.id, "passed": c.passed, "detail": c.detail} for c in r.cases],
                "notes": r.notes,
            }
        )
    return {"suites": suites}
