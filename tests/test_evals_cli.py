"""eval CLI(--write-baseline/--baseline 往返)测试(#148 评审 P1)。

锁死评审闸抓到的 P0:write-baseline 产物(suites 包装)必须能直接作为
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

    # ② 指标腰斩后,用同一份基线文件对比 → 必须 FAIL(评审 P0 的静默空转场景)
    degraded = {"fake": _spec_with({"recall_at_3": 0.40, "mrr": 0.30})}
    rc = await mod._run(_ns(baseline=baseline_path), evals_dir=tree, registry=degraded)
    assert rc == 1

    # ③ 指标持平 → PASS
    rc = await mod._run(_ns(baseline=baseline_path), evals_dir=tree, registry=registry)
    assert rc == 0
