"""统一 eval runner 入口(#148,OBS-03)。

用法:
  uv run python evals/run_evals.py                      # 全量(cases + datasets)
  uv run python evals/run_evals.py --suite kb_probes    # 单 suite
  uv run python evals/run_evals.py --only datasets      # 按面过滤
  uv run python evals/run_evals.py --distribution       # 打分分布(只看不设门,退出码恒 0)
  uv run python evals/run_evals.py --baseline evals/baselines.json      # 对比基线,低于即 FAIL
  uv run python evals/run_evals.py --write-baseline evals/baselines.json # 落当前指标为新基线
  uv run python evals/run_evals.py --json evals/last_run.json           # 机器可读报告

退出码:0 全过 | 1 有 FAIL | 2 全部 SKIP(环境未配置)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from official_agent.evals import dump_json, exit_code, print_report, run_suites

EVALS_DIR = Path(__file__).resolve().parent


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统一 eval runner(#148)")
    parser.add_argument("--only", choices=["cases", "datasets"], help="按面过滤")
    parser.add_argument("--suite", help="按 suite 名(文件 stem)过滤")
    parser.add_argument("--distribution", action="store_true", help="分布模式:只看不设门")
    parser.add_argument("--baseline", type=Path, help="基线文件:指标低于基线即 FAIL")
    parser.add_argument("--write-baseline", type=Path, help="把本次指标写入基线文件")
    parser.add_argument("--json", type=Path, help="机器可读报告输出路径")
    return parser.parse_args(argv)


async def _run(
    args: argparse.Namespace,
    *,
    evals_dir: Path = EVALS_DIR,
    registry: dict[str, Any] | None = None,
) -> int:
    baseline = None
    if args.baseline:
        loaded = json.loads(args.baseline.read_text(encoding="utf-8"))
        # --write-baseline 产物是 {recorded_at, suites:{...}} 包装;兼容裸 {name:...}
        baseline = loaded.get("suites", loaded) if isinstance(loaded, dict) else loaded

    results = await run_suites(
        evals_dir,
        only=args.only,
        suite=args.suite,
        registry=registry,
        distribution=args.distribution,
        baseline=baseline,
    )
    print_report(results)

    if args.json:
        args.json.write_text(
            json.dumps(dump_json(results), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"json report → {args.json}")

    if args.write_baseline:
        payload = {
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "suites": {
                r.name: {"metrics": dict(r.metrics), "status": r.status} for r in results
            },
        }
        args.write_baseline.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"baseline → {args.write_baseline}")

    if args.distribution:
        print("distribution 模式:不设门,退出码恒 0")
        return 0
    return exit_code(results)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
