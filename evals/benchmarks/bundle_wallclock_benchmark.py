"""出题 bundle 的墙钟对照:各证据线逐条跑 vs 并发跑(模拟耗时,不调模型)。

每条线的耗时用 sleep 模拟,所以本脚本量的只是**编排**带来的墙钟差:同一组
线耗时下,整包墙钟是「各线之和」还是「最慢的一批」。它回答不了真实模型下的
耗时与题库质量——那要拿同一批简历在改动前后各跑一遍真实初筛,对比 runner
日志里 `eval_event stage=qbank_done` 的 `duration_ms`,以及
`agent_conversation_log`(channel='evaluation')里的四列 token。

不需要模型 key、GitHub 网络或 Postgres;仅参考不落档,CI 不跑。
用法:uv run python evals/benchmarks/bundle_wallclock_benchmark.py [--repos 5] [--line-seconds 0.4]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from official_agent.evaluation import bundle  # noqa: E402
from official_agent.evaluation.scoring import FieldText  # noqa: E402

#: 评测线模拟耗时相对仓线的比例:评测线只有一次取数加一次出题,远短于一次仓探索
_AUTOGRADING_RATIO = 0.25


class _ReadableGitHub:
    """可读性探测的替身:所有仓都可读,不发网络请求。"""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        pass

    async def repo(self, owner: str, repo: str) -> dict[str, Any]:
        return {"default_branch": "main"}


def _line_envelope() -> dict[str, Any]:
    """带一道入口题的仓线信封:有题,兜底线就不会触发。"""
    return {
        "mode": "repo_deep_dive",
        "group": {
            "entry": {"question": "为什么做这个项目?", "time_minutes": 3},
            "chains": [],
            "reserves": [],
        },
    }


async def _timed_bundle(repos: int, line_seconds: float) -> float:
    """跑一次 bundle(各线耗时为模拟值),返回墙钟秒数。"""

    async def _investigation(_project_text: str, **_kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(line_seconds)
        return _line_envelope()

    async def _autograding(_github_key: str, _resume_id: int) -> None:
        await asyncio.sleep(line_seconds * _AUTOGRADING_RATIO)

    fields = [
        FieldText(
            field_key="project",
            title="项目经历",
            value=" ".join(f"https://github.com/me/repo{i}" for i in range(repos)),
        )
    ]
    with (
        mock.patch.object(bundle, "GitHubClient", _ReadableGitHub),
        mock.patch.object(bundle.ig, "run_investigation", _investigation),
        mock.patch.object(bundle, "_autograding_line", _autograding),
    ):
        started = time.monotonic()
        await bundle.run_bundle(fields, resume_id=0, cycle_id=0, github_key="benchmark")
        return time.monotonic() - started


async def _main(repos: int, line_seconds: float) -> None:
    lines = min(repos, bundle.MAX_REPO_LINES)
    one_at_a_time = lines * line_seconds + line_seconds * _AUTOGRADING_RATIO
    concurrent = await _timed_bundle(repos, line_seconds)
    print(f"仓线 {lines} 条(每条模拟 {line_seconds:.2f}s)+ 评测线 1 条")
    print(f"  逐条跑(各线耗时之和):         {one_at_a_time:6.2f}s")
    print(
        f"  并发跑(仓线至多 {bundle.MAX_PARALLEL_REPO_LINES} 条同时在跑):"
        f" {concurrent:6.2f}s  →  {one_at_a_time / concurrent:.1f}x"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repos", type=int, default=5, help="简历里的 GitHub 仓数")
    parser.add_argument("--line-seconds", type=float, default=0.4, help="每条仓线的模拟耗时")
    args = parser.parse_args()
    asyncio.run(_main(args.repos, args.line_seconds))
