"""eval 引擎单测(#148):发现/分发/退出码/基线对比,全 fake executor。"""

import json
from pathlib import Path

import pytest

from official_agent.evals.engine import (
    SuiteResult,
    discover_suites,
    exit_code,
    run_suites,
)


def _spec(status: str, metrics: dict[str, float] | None = None):
    """构造固定结果的 fake spec。"""

    async def run(path: Path, *, distribution: bool = False) -> SuiteResult:
        return SuiteResult(
            name=path.stem,
            kind="fake",
            source=path.name,
            status=status,
            metrics=dict(metrics or {}),
        )

    def blocker() -> str | None:
        return None

    from official_agent.evals.engine import SuiteSpec

    return SuiteSpec(run=run, env_blocker=blocker)


def _make_tree(tmp_path: Path) -> Path:
    """合成 evals 目录:一个 cases 文件 + 一个 datasets 文件,均显式 runner: fake。"""
    (tmp_path / "cases").mkdir()
    (tmp_path / "datasets").mkdir()
    (tmp_path / "cases" / "foo.yaml").write_text(
        "runner: fake\ncases:\n  - id: x\n", encoding="utf-8"
    )
    (tmp_path / "datasets" / "bar.yaml").write_text("runner: fake\nprobes: []\n", encoding="utf-8")
    return tmp_path


def test_discover_reads_both_areas_and_honors_runner_field(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    refs = discover_suites(tree)
    assert [(r.name, r.area, r.kind) for r in refs] == [
        ("foo", "cases", "fake"),
        ("bar", "datasets", "fake"),
    ]


def test_sniff_legacy_files_without_runner_field(tmp_path: Path) -> None:
    """存量两份文件无 runner: 字段,按结构嗅探。"""
    (tmp_path / "cases").mkdir()
    (tmp_path / "datasets").mkdir()
    (tmp_path / "cases" / "ts.yaml").write_text(
        "- id: ts-001\n  expected_tools: [a]\n", encoding="utf-8"
    )
    (tmp_path / "datasets" / "kb.yaml").write_text(
        "defaults: {}\nprobes:\n  - id: p\n", encoding="utf-8"
    )
    kinds = {r.name: r.kind for r in discover_suites(tmp_path)}
    assert kinds == {"ts": "tool_selection", "kb": "kb_probes"}


def test_sniff_unknown_structure_is_loud(tmp_path: Path) -> None:
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "mystery.yaml").write_text("defaults: {}\nitems: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="runner"):
        discover_suites(tmp_path)


async def test_run_suites_aggregates_and_filters(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    registry = {"fake": _spec("PASS", {"m": 1.0})}

    results = await run_suites(tree, registry=registry)
    assert [r.status for r in results] == ["PASS", "PASS"]
    assert exit_code(results) == 0

    only_datasets = await run_suites(tree, registry=registry, only="datasets")
    assert [r.name for r in only_datasets] == ["bar"]

    single = await run_suites(tree, registry=registry, suite="foo")
    assert [r.name for r in single] == ["foo"]


async def test_exit_codes_fail_and_all_skip(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    fail_results = await run_suites(tree, registry={"fake": _spec("FAIL")})
    assert exit_code(fail_results) == 1

    from official_agent.evals.engine import SuiteSpec

    async def run(path: Path, *, distribution: bool = False) -> SuiteResult:
        return SuiteResult(name=path.stem, kind="fake", source=path.name, status="SKIP")

    def blocker() -> str | None:
        return "没有配置"

    skip_reg = {"fake": SuiteSpec(run=run, env_blocker=blocker)}
    skip_results = await run_suites(tree, registry=skip_reg)
    assert all(r.status == "SKIP" for r in skip_results)
    assert exit_code(skip_results) == 2  # 全 SKIP 不可静默变绿


async def test_env_blocker_produces_skip_with_note(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    from official_agent.evals.engine import SuiteSpec

    async def run(path: Path, *, distribution: bool = False) -> SuiteResult:  # pragma: no cover
        raise AssertionError("环境未配置时不应执行 executor")

    def blocker() -> str | None:
        return "EMBED_* 未配置"

    results = await run_suites(tree, registry={"fake": SuiteSpec(run=run, env_blocker=blocker)})
    assert [r.status for r in results] == ["SKIP", "SKIP"]
    assert "EMBED_* 未配置" in results[0].notes[0]


async def test_unknown_kind_raises_loud(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    (tree / "cases" / "weird.yaml").write_text(
        "runner: nobody\ncases:\n  - id: x\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="nobody"):
        await run_suites(tree, registry={"fake": _spec("PASS")})


async def test_baseline_regression_flags_fail(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path)
    registry = {"fake": _spec("PASS", {"recall_at_3": 0.90, "mrr": 0.80})}

    baseline = {
        "foo": {"metrics": {"recall_at_3": 0.95, "mrr": 0.75}},  # recall 门槛高于当前
        "bar": {"metrics": {"recall_at_3": 0.85, "mrr": 0.75}},  # 当前全达标
    }
    results = await run_suites(tree, registry=registry, baseline=baseline)
    by_name = {r.name: r for r in results}
    assert by_name["foo"].status == "FAIL"  # recall 0.90 < 基线 0.95 → 回归
    assert any("REGRESSION" in n for n in by_name["foo"].notes)
    assert by_name["bar"].status == "PASS"  # 0.90 ≥ 0.85 / 0.80 ≥ 0.75 → 不动

    # 指标持平基线不算回归(严格小于才触发)
    same = await run_suites(
        tree,
        registry={"fake": _spec("PASS", {"recall_at_3": 0.95, "mrr": 0.80})},
        baseline=baseline,
    )
    assert all(r.status == "PASS" for r in same)


async def test_pass_rate_metric_injected_for_case_suites(tmp_path: Path) -> None:
    """有 cases 的 suite,pass_rate 进 metrics(基线对比的统一抓手)。"""
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "c.yaml").write_text(
        "runner: fake\ncases:\n  - id: x\n", encoding="utf-8"
    )
    from official_agent.evals.engine import CaseResult, SuiteSpec

    async def run(path: Path, *, distribution: bool = False) -> SuiteResult:
        return SuiteResult(
            name=path.stem,
            kind="fake",
            source=path.name,
            status="FAIL",
            cases=[CaseResult(id="a", passed=True), CaseResult(id="b", passed=False)],
        )

    def blocker() -> str | None:
        return None

    results = await run_suites(tmp_path, registry={"fake": SuiteSpec(run=run, env_blocker=blocker)})
    assert results[0].metrics["pass_rate"] == pytest.approx(0.5)


def test_dump_json_shape(tmp_path: Path) -> None:
    from official_agent.evals.engine import dump_json

    result = SuiteResult(name="s", kind="k", source="cases/s.yaml", status="PASS")
    payload = dump_json([result])
    assert payload["suites"][0]["name"] == "s"
    json.dumps(payload)  # 可序列化
