"""出题探针 executor(#155,spec §7):六探针确定性门禁。

数据集 evals/datasets/qbank_probes.yaml:每条 case 喂
validate_qbank_v2_group(与 generate 主路径同一校验机器)——
expect=pass 须通过;expect=reject 须以 match 子串拒绝。无 LLM 依赖,
进 CI 也可跑。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evaluation.investigate_graph import validate_qbank_v2_group


def _load_yaml(path: Path) -> Any:
    """同步读盘抽小函数(ASYNC240:异步体内不做阻塞 IO)。"""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None  # 纯确定性校验,无环境依赖


async def run_suite(path: Path, *, distribution: bool = False, **_: Any) -> SuiteResult:
    data = _load_yaml(path) or {}
    fixtures = data.get("fixtures", {}) if isinstance(data, dict) else {}
    dossier = str(fixtures.get("dossier", ""))
    paths = list(fixtures.get("paths", []))
    cases = data.get("cases", []) if isinstance(data, dict) else []

    results = []
    for case in cases:
        cid = str(case.get("id", "?"))
        expect = str(case.get("validate", "pass"))
        match = str(case.get("match", ""))
        # case 结构:{id, validate, match?, thin?, group:{entry/chains/reserves}}
        group_payload = case.get("group") or {}
        payload = {
            k: v
            for k, v in (
                ("entry", group_payload.get("entry")),
                ("chains", group_payload.get("chains")),
                ("reserves", group_payload.get("reserves")),
            )
            if v is not None
        }
        thin = bool(case.get("thin", False))
        try:
            validate_qbank_v2_group(payload, dossier, paths=paths, thin=thin)
            passed, detail = expect == "pass", "校验通过(期望通过)"
        except ValueError as exc:
            message = str(exc)
            if expect == "reject":
                ok = (not match) or match in message
                passed = ok
                detail = f"按预期拒绝:{message[:80]}"
            else:
                passed, detail = False, f"期望通过但被拒:{message[:80]}"
        results.append(CaseResult(id=cid, passed=passed, detail=detail))

    return SuiteResult(
        name=path.stem,
        kind="qbank_probes",
        source=path.name,
        status="FAIL" if any(not c.passed for c in results) else "PASS",
        cases=results,
    )
