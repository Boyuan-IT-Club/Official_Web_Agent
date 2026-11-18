"""对话意图提示门禁执行器:逐条跑规则判断与提示渲染,断言标签、标记与提示边界。

数据集 evals/datasets/intent_probes.yaml。纯确定性:不调模型、不连后端,随 pytest 进 PR 检查。

两层断言:
- 期望(expect):标签、标记集合、是否不确定、建议工具、承接选中第几项;
- 不变量(每条都查,不写在 expect 里):提示文本只点名该身份已装配的工具;
  判为不确定时不出提示。

切分:dev 用例全部通过才算 PASS;holdout 的通过率不低于 defaults.holdout_min_pass_rate。
同一 group 跨了 split 直接 FAIL——改写句分进两边,验收集就不再独立。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.graphs.assistant import intent as turn_intent
from official_agent.graphs.identity import ResolvedIdentity

_SPLITS = ("dev", "holdout")


def env_blocker() -> str | None:
    return None


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _identity(case: dict[str, Any], defaults: dict[str, Any]) -> ResolvedIdentity:
    role = str(case.get("role", "admin"))
    codes = case.get("permission_codes")
    if codes is None and role == "admin":
        codes = defaults.get("admin_permission_codes") or []
    return cast(
        ResolvedIdentity,
        {"user_id": 7, "role": role, "permission_codes": list(codes or []), "source": "eval"},
    )


def _prior_options(spec: dict[str, Any] | None) -> tuple[turn_intent.PriorOption, ...]:
    """合成上一轮的选项:id 从 5 起递增(与 tool_selection 的假后端场次号对齐)。"""
    if not spec:
        return ()
    kind = str(spec["kind"])
    return tuple(
        turn_intent.PriorOption(kind, 5 + i, f"#{5 + i}") for i in range(int(spec["count"]))
    )


def check_case(case: dict[str, Any], defaults: dict[str, Any]) -> list[str]:
    """一条用例 → 不满足的条目(空表 = 通过)。纯函数,单测直接喂用例。"""
    from official_agent.graphs.assistant import tool_roster

    options = _prior_options(case.get("prior_options"))
    result = turn_intent.classify_turn(
        str(case["input"]),
        prior_options=options,
        cancelled_pending=bool(case.get("cancelled_pending")),
    )
    expect = case.get("expect") or {}
    problems: list[str] = []
    if result.label != expect.get("label"):
        problems.append(f"label={result.label} 期望 {expect.get('label')}")
    want_flags = set(expect.get("flags") or [])
    if set(result.flags) != want_flags:
        problems.append(f"flags={sorted(result.flags)} 期望 {sorted(want_flags)}")
    if result.uncertain != bool(expect.get("uncertain", False)):
        problems.append(f"uncertain={result.uncertain}")
    missing_tools = set(expect.get("tools") or []) - set(result.suggested_tools)
    if missing_tools:
        problems.append(f"建议工具缺 {sorted(missing_tools)};实际 {list(result.suggested_tools)}")
    if "chosen_option" in expect and result.chosen_ordinal != expect["chosen_option"]:
        problems.append(f"选中第 {result.chosen_ordinal} 项,期望第 {expect['chosen_option']} 项")

    assembled = set(tool_roster(_identity(case, defaults), allow_write=True))
    hint = turn_intent.render_hint(result, assembled)
    leaked = turn_intent.mentioned_tools(hint) - assembled
    if leaked:
        problems.append(f"提示点名了未装配的工具 {sorted(leaked)}")
    if result.uncertain and hint:
        problems.append("判为不确定却出了提示")
    return problems


def _group_leaks(cases: list[dict[str, Any]]) -> list[str]:
    splits: dict[str, set[str]] = {}
    for case in cases:
        splits.setdefault(str(case.get("group")), set()).add(str(case.get("split")))
    return sorted(group for group, seen in splits.items() if len(seen) > 1)


def _rate(cases: list[CaseResult]) -> float:
    return sum(1 for c in cases if c.passed) / len(cases) if cases else 1.0


async def run_suite(path: Path, *, distribution: bool = False, **_: Any) -> SuiteResult:
    data = _load_yaml(path) or {}
    defaults = data.get("defaults") or {}
    raw_cases = list(data.get("cases") or [])
    min_holdout = float(defaults.get("holdout_min_pass_rate", 0.8))

    by_split: dict[str, list[CaseResult]] = {split: [] for split in _SPLITS}
    label_hits = dict.fromkeys(_SPLITS, 0)
    for case in raw_cases:
        split = str(case.get("split"))
        if split not in by_split:
            raise ValueError(f"{case.get('id')}: split 只能是 {_SPLITS}")
        try:
            problems = check_case(case, defaults)
        except Exception as exc:  # noqa: BLE001 — 单条炸了记 FAIL,不中止整套
            problems = [f"{type(exc).__name__}: {exc}"[:160]]
        if not any(p.startswith("label=") for p in problems):
            label_hits[split] += 1
        by_split[split].append(
            CaseResult(id=str(case["id"]), passed=not problems, detail=";".join(problems))
        )

    leaks = _group_leaks(raw_cases)
    dev_rate, holdout_rate = _rate(by_split["dev"]), _rate(by_split["holdout"])
    holdout_n = len(by_split["holdout"])
    failed = bool(leaks) or dev_rate < 1.0 or holdout_rate < min_holdout
    notes = [
        f"dev {sum(c.passed for c in by_split['dev'])}/{len(by_split['dev'])}(须全过)",
        f"holdout {sum(c.passed for c in by_split['holdout'])}/{holdout_n},门槛 {min_holdout}",
    ]
    if leaks:
        notes.append(f"group 跨了 split:{leaks}")
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return SuiteResult(
        name=path.stem,
        kind="intent_probes",
        source=path.name,
        status="FAIL" if failed and not distribution else "PASS",
        cases=[*by_split["dev"], *by_split["holdout"]],
        metrics={
            "dev_pass_rate": round(dev_rate, 3),
            "holdout_pass_rate": round(holdout_rate, 3),
            "holdout_label_accuracy": round(label_hits["holdout"] / holdout_n, 3)
            if holdout_n
            else 1.0,
        },
        notes=notes,
        context={
            "dataset_sha": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
            "intent_version": turn_intent.INTENT_VERSION,
            "hint_prompt": turn_intent.hint_prompt_version(),
        },
    )
