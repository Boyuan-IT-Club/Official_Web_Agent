"""eval CLI(--write-baseline/--baseline 往返)测试。

锁死基线门禁的静默空转:write-baseline 产物(suites 包装)必须能直接作为
--baseline 生效——指标回退时门禁必须 FAIL,不允许静默变绿。
"""

import argparse
import importlib.util
import json
from pathlib import Path

from official_agent.evals.engine import SuiteResult, SuiteSpec


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "run_evals_cli", Path(__file__).resolve().parents[1] / "evals" / "run_evals.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spec_with(metrics: dict[str, float]):
    async def run(path: Path, *, distribution: bool = False) -> SuiteResult:
        return SuiteResult(
            name=path.stem,
            kind="fake",
            source=path.name,
            status="PASS",
            metrics=dict(metrics),
        )

    def blocker() -> str | None:
        return None

    return SuiteSpec(run=run, env_blocker=blocker)


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "foo.yaml").write_text(
        "runner: fake\ncases:\n  - id: x\n", encoding="utf-8"
    )
    return tmp_path


def _ns(**kwargs: object) -> argparse.Namespace:
    defaults = dict(
        only=None,
        suite=None,
        distribution=False,
        baseline=None,
        write_baseline=None,
        json=None,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


async def test_write_then_regress_baseline_gate_fails(tmp_path: Path) -> None:
    mod = _load_cli()
    tree = _tree(tmp_path)
    registry = {"fake": _spec_with({"recall_at_3": 0.95, "mrr": 0.80})}
    baseline_path = tmp_path / "baselines.json"

    # ① 写基线:产物是 {recorded_at, suites} 包装,里面含本次指标
    rc = await mod._run(_ns(write_baseline=baseline_path), evals_dir=tree, registry=registry)
    assert rc == 0
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert "suites" in payload and "recall_at_3" in payload["suites"]["foo"]["metrics"]

    # ② 指标腰斩后,用同一份基线文件对比 → 必须 FAIL(防静默空转)
    degraded = {"fake": _spec_with({"recall_at_3": 0.40, "mrr": 0.30})}
    rc = await mod._run(_ns(baseline=baseline_path), evals_dir=tree, registry=degraded)
    assert rc == 1

    # ③ 指标持平 → PASS
    rc = await mod._run(_ns(baseline=baseline_path), evals_dir=tree, registry=registry)
    assert rc == 0


async def test_baseline_file_keeps_observations_and_context(tmp_path: Path) -> None:
    """基线要能回答「这些数是在什么条件下量的、当时花了多少」,不只是指标本身。"""
    mod = _load_cli()
    tree = _tree(tmp_path)

    async def run(path: Path, *, distribution: bool = False) -> SuiteResult:
        return SuiteResult(
            name=path.stem,
            kind="fake",
            source=path.name,
            status="PASS",
            metrics={"item_stability": 0.9},
            observations={"avg_input_tokens": 3200.0},
            context={"repeats": 3, "model": "fake-strong"},
        )

    registry = {"fake": SuiteSpec(run=run, env_blocker=lambda: None)}
    # 目录不存在也能写:各模式的基线各放一个文件,通常落在子目录里
    baseline_path = tmp_path / "baselines" / "foo.single.json"

    rc = await mod._run(_ns(write_baseline=baseline_path), evals_dir=tree, registry=registry)

    assert rc == 0
    entry = json.loads(baseline_path.read_text(encoding="utf-8"))["suites"]["foo"]
    assert entry["metrics"] == {"item_stability": 0.9}
    assert entry["observations"] == {"avg_input_tokens": 3200.0}
    assert entry["context"] == {"repeats": 3, "model": "fake-strong"}


async def test_skipped_suite_never_becomes_a_baseline(tmp_path: Path) -> None:
    """没跑的 suite 没有指标;把它记成基线,以后任何结果都「不低于基线」,门禁就空转了。"""
    mod = _load_cli()
    tree = _tree(tmp_path)

    async def never(path: Path, *, distribution: bool = False) -> SuiteResult:
        raise AssertionError("环境未配置时不应执行")

    skipped = {"fake": SuiteSpec(run=never, env_blocker=lambda: "LLM_* 未配置")}
    baseline_path = tmp_path / "baselines.json"

    rc = await mod._run(_ns(write_baseline=baseline_path), evals_dir=tree, registry=skipped)
    assert rc == 2
    assert not baseline_path.exists()

    # 已有的基线不会被一次全 SKIP 的运行冲掉
    baseline_path.write_text('{"suites": {"foo": {"metrics": {"m": 0.9}}}}', encoding="utf-8")
    await mod._run(_ns(write_baseline=baseline_path), evals_dir=tree, registry=skipped)
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["suites"]["foo"]["metrics"] == {
        "m": 0.9
    }
