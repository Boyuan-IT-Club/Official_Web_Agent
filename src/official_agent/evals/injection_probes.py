"""注入探针 executor(#163):scan 段确定性检测;model 段显式 SKIP。

injection_probes.yaml 的 kind=injection_probes。#159 决议 §4:LLM 行为断言
(评分卡基线一致/system 不泄漏)等 judge runner(#155 同批)接线后执行,
本执行器先把「检测标注正确」钉成确定性门禁。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.security.injection_guard import scan_injection


def _load_yaml(path: Path) -> Any:
    """同步读盘抽小函数(ASYNC240:异步体内不做阻塞 IO)。"""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None  # 纯确定性扫描,无环境依赖


async def run_suite(
    path: Path,
    *,
    distribution: bool = False,
    **_: Any,
) -> SuiteResult:
    """逐 case:scan 段断言检测与期望一致;model 段 SKIP(judge runner 待接)。"""
    data = _load_yaml(path) or []
    # 显式格式 {runner, cases:[...]};裸列表为早期格式兼容
    cases = data.get("cases", []) if isinstance(data, dict) else data
    results = []
    skipped_model = 0
    for case in cases:
        cid = str(case.get("id", "?"))
        kind = str(case.get("kind", "scan"))
        if kind != "scan":
            skipped_model += 1
            results.append(
                CaseResult(
                    id=cid,
                    passed=True,
                    skipped=True,  # 不计 pass_rate 分母
                    detail="model 段待 judge runner(#155 同批)",
                )
            )
            continue
        text = str(case.get("text", ""))
        expect = bool(case.get("expect_detect", False))
        hit, matched = scan_injection(text)
        passed = hit == expect
        detail = f"命中 {matched!r}" if hit else "未命中(期望)" if expect else "未命中(符合预期)"
        results.append(CaseResult(id=cid, passed=passed, detail=detail))

    failures = [c for c in results if not c.passed and not c.skipped]
    return SuiteResult(
        name=path.stem,
        kind="injection_probes",
        source=path.name,
        status="FAIL" if failures else "PASS",
        cases=results,
        notes=[f"model 段 {skipped_model} 条待 judge runner,本批不计分"] if skipped_model else [],
    )
