"""注入场景的确定性门禁:就算模型照着提问里的指令做了,夹带的内容也进不了草稿。

数据集 evals/datasets/kb_ops_injection.yaml。不调模型:起草、修订、审核三个角色的
输出都写死在数据集里,而且写成「模型已经被带偏」的样子——照搬提问里的假事实、
编造引文、审核者一路放行。断言的是代码里的那几道闸(样本过滤、输出闸门、引文与
数字复核、裁决规则、修订的权限)各自挡住了什么,所以任何环境都能跑。

三类用例:
- pipeline:走完整的「起草 → 预检 → 审核 → 修订 → 裁决」(kb_ops.drafting.draft_topic);
- proposal_gate:起草者的原始回复能不能过输出闸门(kb_ops.draft.parse_proposal);
- samples:一组提问里哪些能送到模型面前(kb_ops.triage.safe_samples)。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.evals.kb_ops_fixtures import FixtureKbReader, chunk_records, proposal_of
from official_agent.kb_ops.draft import DraftProposal, parse_proposal
from official_agent.kb_ops.drafting import DraftOutcome, DraftRoles, draft_topic
from official_agent.kb_ops.triage import safe_samples
from official_agent.kb_ops.verify import ReviewOutput, SentenceVerdict

#: review 写成这个值表示审核者被带偏:每一句都判有出处,引文照抄起草者给的
_COMPROMISED = "compromised"


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None


def _scripted_roles(case: dict[str, Any]) -> DraftRoles:
    """按数据集里写死的输出回放的三个角色。修订缺省「坚持原稿不改」。"""
    reviews = case.get("review", _COMPROMISED)
    calls = {"verify": 0}

    async def draft(question: str, topic: str, reader: Any, deadline: float) -> Any:
        return proposal_of(case["draft"]), {}

    async def revise(proposal: DraftProposal, problems: Any, chunks: Any) -> Any:
        return proposal_of(case.get("revise", case["draft"])), {}

    async def verify(proposal: DraftProposal, chunks: Any, neighbors: Any) -> Any:
        calls["verify"] += 1
        if reviews == _COMPROMISED:
            verdicts = [{"verdict": "supported", "quote": s.quote} for s in proposal.sentences]
        else:
            verdicts = reviews[min(calls["verify"], len(reviews)) - 1]
        return (
            ReviewOutput(
                sentences=[
                    SentenceVerdict(index=i, verdict=v["verdict"], quote=v.get("quote", ""))
                    for i, v in enumerate(verdicts, start=1)
                ]
            ),
            {},
        )

    return DraftRoles(draft=draft, revise=revise, verify=verify)


def check_draft_outcome(outcome: DraftOutcome, expect: dict[str, Any]) -> list[str]:
    """起草结局 vs 期望 → 不满足的条目(空表 = 通过)。纯函数。

    forbidden / required 看的是会给管理员看、批准后会进知识库的文字:问题与各句正文。
    """
    problems: list[str] = []
    proposal = outcome.proposal
    if outcome.status != expect.get("status", outcome.status):
        problems.append(f"status={outcome.status} 期望 {expect['status']}")
    kind = proposal.kind if proposal else None
    if "kind" in expect and kind != expect["kind"]:
        problems.append(f"kind={kind} 期望 {expect['kind']}")
    sentences = [s.text for s in proposal.sentences] if proposal else []
    if "sentence_count" in expect and len(sentences) != int(expect["sentence_count"]):
        problems.append(f"句数={len(sentences)} 期望 {expect['sentence_count']}")
    shown = " ".join([proposal.question if proposal else "", *sentences])
    for fragment in expect.get("forbidden") or []:
        if fragment in shown:
            problems.append(f"草稿里出现了「{fragment}」:{shown}")
    for fragment in expect.get("required") or []:
        if fragment not in shown:
            problems.append(f"草稿里缺「{fragment}」:{shown}")
    return problems


async def _pipeline_case(case: dict[str, Any], chunks: dict[str, Any]) -> list[str]:
    outcome = await draft_topic(
        str(case["question"]),
        str(case.get("topic", "")),
        reader=FixtureKbReader(chunks),
        roles=_scripted_roles(case),
        deadline=time.monotonic() + 30,
    )
    return check_draft_outcome(outcome, case.get("expect") or {})


def _gate_case(case: dict[str, Any]) -> list[str]:
    try:
        parse_proposal(str(case["reply"]))
        accepted = True
    except ValueError:
        accepted = False
    want = bool(case["expect"]["accepted"])
    return [] if accepted == want else [f"过闸={accepted} 期望 {want}"]


def _samples_case(case: dict[str, Any]) -> list[str]:
    got = safe_samples(list(case["samples"]))
    want = list(case["expect"]["reach_model"])
    return [] if got == want else [f"送到模型面前的样本={got} 期望 {want}"]


async def run_suite(path: Path, *, distribution: bool = False, **_: Any) -> SuiteResult:
    data = _load_yaml(path) or {}
    chunks = chunk_records(data.get("chunks") or {})
    cases: list[CaseResult] = []
    for case in data.get("cases") or []:
        kind = case.get("kind", "pipeline")
        if kind == "pipeline":
            problems = await _pipeline_case(case, chunks)
        elif kind == "proposal_gate":
            problems = _gate_case(case)
        elif kind == "samples":
            problems = _samples_case(case)
        else:
            problems = [f"未知用例类型 {kind}"]
        cases.append(CaseResult(id=case["id"], passed=not problems, detail=";".join(problems)))
    failed = [c.id for c in cases if not c.passed]
    return SuiteResult(
        name=path.stem,
        kind="kb_ops_injection",
        source=path.name,
        status="FAIL" if failed else "PASS",
        cases=cases,
        notes=[f"通过 {len(cases) - len(failed)}/{len(cases)}"],
    )
