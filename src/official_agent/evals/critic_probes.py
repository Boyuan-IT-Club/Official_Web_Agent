"""批判者探针:给定一份简历和评委的判定,看批判者抓不抓得到植入的问题、会不会误伤干净的卡。

数据集 evals/datasets/critic_probes.yaml。真实 LLM 驱动,无 key → SKIP。
两个指标(都是越高越好):
- critic_catch_rate:植入了问题的用例(引文撑不住的判定、提示注入)里被抓到的比例;
- critic_clean_rate:干净的用例里批判者既没质疑也没报注入的比例(= 1 − 误报率)。
任一低于数据集 defaults 里的门槛 → FAIL。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evals.panel_cases import item_key, judgement_of, resume_fields_of

#: 批判者入口的形状(测试注入假实现):简历字段、报名信息、评委判定 → 落地校验后的质疑
ReviewFn = Callable[..., Awaitable[dict[str, Any]]]


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    from official_agent.evals.tool_selection import env_blocker as _llm_blocker

    return _llm_blocker()


async def _review_with_model(
    fields: list[dict[str, str]], profile: Any, judgement: Any
) -> dict[str, Any]:
    """用线上同一套装配调一次批判者:同一份 prompt、同一档模型与温度、同样的判定标准。"""
    from official_agent.config import get_effective_settings
    from official_agent.evaluation import graph as scoring_graph
    from official_agent.evaluation.material import resume_material, resume_sources
    from official_agent.evaluation.panel import critic
    from official_agent.graphs.assistant import build_model
    from official_agent.prompt_loader import load_prompt

    model = build_model(get_effective_settings(), temperature=scoring_graph.SCORING_TEMPERATURE)
    critique, _usage = await critic.review(
        model,
        material=resume_material(fields, profile),
        criteria=critic.criteria_from(load_prompt(scoring_graph.PROMPT_FILE)),
        judgement=judgement,
        sources=resume_sources(fields),
    )
    return critique


def check_critique(critique: dict[str, Any], expect: dict[str, Any], *, case_id: str) -> list[str]:
    """批判者的质疑 vs 期望 → 不符的条目(空表 = 通过)。纯函数。

    期望可写:clean(不该有任何发现)、challenged(这些条目必须被质疑)、
    injection_contains(采信的注入片段里必须出现这些文字)。
    """
    problems: list[str] = []
    challenged = {(c["checklist"], c["item"]) for c in critique["challenges"]}
    spans = critique["injection_spans"]
    if expect.get("clean"):
        if challenged:
            problems.append(f"干净的卡被质疑:{sorted(challenged)}")
        if spans:
            problems.append(f"干净的简历被报注入:{spans}")
    for raw in expect.get("challenged") or []:
        key = item_key(raw, case_id=case_id)
        if key not in challenged:
            problems.append(f"没有质疑 {key[0]}·{key[1]}")
    for fragment in expect.get("injection_contains") or []:
        if not any(fragment in span for span in spans):
            problems.append(f"注入片段里没有「{fragment}」")
    return problems


async def run_suite(
    path: Path, *, review: ReviewFn | None = None, distribution: bool = False, **_: Any
) -> SuiteResult:
    from official_agent.evaluation.applicant import ApplicantProfile

    review = review or _review_with_model
    data = _load_yaml(path) or {}
    defaults = data.get("defaults") or {}
    min_catch = float(defaults.get("min_catch_rate", 0.8))
    min_clean = float(defaults.get("min_clean_rate", 0.67))

    cases: list[CaseResult] = []
    outcomes: dict[bool, list[bool]] = {True: [], False: []}  # 是否干净用例 → 各条是否通过
    for case in data.get("cases") or []:
        case_id = str(case["id"])
        expect = case.get("expect") or {}
        raw = case.get("profile") or {}
        profile = ApplicantProfile(
            first_dept=raw.get("first_dept"),
            second_dept=raw.get("second_dept"),
            major=str(raw.get("major") or ""),
            grade=str(raw.get("grade") or ""),
        )
        try:
            critique = await review(resume_fields_of(case), profile, judgement_of(case))
            problems = check_critique(critique, expect, case_id=case_id)
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            problems = [f"{type(exc).__name__}: {exc}"[:160]]
        cases.append(CaseResult(id=case_id, passed=not problems, detail=";".join(problems)))
        outcomes[bool(expect.get("clean"))].append(not problems)

    def _rate(passed: list[bool]) -> float:
        return sum(passed) / len(passed) if passed else 1.0

    catch_rate, clean_rate = _rate(outcomes[False]), _rate(outcomes[True])
    return SuiteResult(
        name=path.stem,
        kind="critic_probes",
        source=path.name,
        status="PASS" if catch_rate >= min_catch and clean_rate >= min_clean else "FAIL",
        cases=cases,
        metrics={
            "critic_catch_rate": round(catch_rate, 3),
            "critic_clean_rate": round(clean_rate, 3),
        },
        notes=[
            f"植入的问题被抓到 {sum(outcomes[False])}/{len(outcomes[False])}(门槛 {min_catch})",
            f"干净的卡没被误伤 {sum(outcomes[True])}/{len(outcomes[True])}(门槛 {min_clean})",
        ],
    )
