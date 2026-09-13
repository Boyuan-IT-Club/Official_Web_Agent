"""通用 eval 体系(#148,OBS-03 收口)。

两个 suite 面:
- ``evals/cases/*.yaml``  :用例断言(给定输入,断言 agent 行为)
- ``evals/datasets/*.yaml``:阈值型门禁(数据集指标 ≥ 基线)

引擎入口 :func:`engine.run_suites`;CLI 入口 ``evals/run_evals.py``。
后续探针(qbank_probes/judge 报告/回归用例)以新 executor kind 挂进注册表,
不改引擎。
"""

from official_agent.evals.engine import (
    CaseResult,
    SuiteResult,
    default_registry,
    discover_suites,
    dump_json,
    exit_code,
    print_report,
    run_suites,
)

__all__ = [
    "CaseResult",
    "SuiteResult",
    "default_registry",
    "discover_suites",
    "dump_json",
    "exit_code",
    "print_report",
    "run_suites",
]
