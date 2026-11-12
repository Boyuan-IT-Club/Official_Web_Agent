"""校准案例探针:入库筛查、按周期冻结与隔离、检索选择这几条规则的验收。

数据集 evals/datasets/calibration_probes.yaml。四种用例:
- screen:   一条判例过入库筛查——含指令样文本的被拒,通过的已脱敏;
- eligible: 给定一批案例与正在评的(水位、周期、简历),哪些可用;
- select:   给定向量,每个判定项选出哪几条判例;
- retrieve: 用真实的嵌入模型,相关的种子判例能不能被检到。
前三种是确定性的,不需要任何环境;retrieve 需要 EMBED_* 配置,没配时单条跳过、
不计入通过率。任何一条不符即 FAIL。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult

#: 嵌入函数的形状(测试注入假实现):一批文本 → 等长的向量列表
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None  # 确定性用例无环境依赖;要嵌入模型的用例各自判断、各自跳过


def _embedding_configured() -> bool:
    from official_agent.config import get_settings

    settings = get_settings()
    return bool(settings.embed_base_url and settings.embed_api_key and settings.embed_model)


def _check_screen(case: dict[str, Any]) -> list[str]:
    from official_agent.evaluation.panel import anchors

    expect = case.get("expect") or {}
    try:
        screened = anchors.screen_case(**case["case"])
    except anchors.CaseRejectedError as exc:
        return [] if expect.get("rejected") else [f"不该被拒:{exc}"]
    if expect.get("rejected"):
        return ["该被拒的判例通过了筛查"]
    stored = f"{screened['quote']}\n{screened['reason']}"
    return [f"入库文本里还有「{raw}」" for raw in expect.get("lacks") or [] if raw in stored]


def _check_eligible(case: dict[str, Any]) -> list[str]:
    from official_agent.evaluation.panel import anchors

    usable = anchors.eligible_cases(case["cases"], **case["scoring"])
    got, want = [c["case_id"] for c in usable], list((case.get("expect") or {}).get("usable", []))
    return [] if got == want else [f"可用案例={got} 期望 {want}"]


def _selected_ids(selected: dict[Any, list[dict[str, Any]]]) -> list[int]:
    return sorted(c["case_id"] for cases in selected.values() for c in cases)


def _check_select(case: dict[str, Any]) -> list[str]:
    from official_agent.evaluation.panel import anchors

    cases = [{**c, "embedding": c["vector"]} for c in case["cases"]]
    got = _selected_ids(anchors.select_anchors(cases, case["fragments"]))
    want = sorted((case.get("expect") or {}).get("selected", []))
    return [] if got == want else [f"选中的案例={got} 期望 {want}"]


async def _check_retrieve(case: dict[str, Any], embed: EmbedFn) -> list[str]:
    """种子判例与简历片段都用真实模型算向量,再走同一个选择函数。案例号 = 种子的序号。"""
    from official_agent.evaluation.panel import anchors

    seeds = [anchors.screen_case(**seed) for seed in case["seeds"]]
    vectors = await embed([anchors.case_text(seed) for seed in seeds] + list(case["resume"]))
    cases = [{**seed, "case_id": i, "embedding": vectors[i]} for i, seed in enumerate(seeds)]
    got = _selected_ids(anchors.select_anchors(cases, vectors[len(seeds) :]))
    expect = case.get("expect") or {}
    problems = [f"第 {i} 条种子判例没被检到" for i in expect.get("retrieved", []) if i not in got]
    problems += [
        f"第 {i} 条种子判例不该被检到" for i in expect.get("not_retrieved", []) if i in got
    ]
    return problems


async def run_suite(
    path: Path, *, embed: EmbedFn | None = None, distribution: bool = False, **_: Any
) -> SuiteResult:
    data = _load_yaml(path) or {}
    can_embed = embed is not None or _embedding_configured()
    if embed is None:
        from official_agent.kb.embedding import embed_texts

        embed = embed_texts

    results: list[CaseResult] = []
    for case in data.get("cases") or []:
        case_id, kind = str(case["id"]), str(case.get("kind"))
        if kind == "retrieve" and not can_embed:
            results.append(
                CaseResult(id=case_id, passed=True, skipped=True, detail="EMBED_* 未配置")
            )
            continue
        try:
            if kind == "screen":
                problems = _check_screen(case)
            elif kind == "eligible":
                problems = _check_eligible(case)
            elif kind == "select":
                problems = _check_select(case)
            elif kind == "retrieve":
                problems = await _check_retrieve(case, embed)
            else:
                raise ValueError(f"用例 {case_id}:未知的 kind {kind!r}")
        except ValueError:
            raise  # 数据集写错直接抛,不当成一条失败的用例
        except Exception as exc:  # noqa: BLE001 — 单条失败计 FAIL,不中止整套
            problems = [f"{type(exc).__name__}: {exc}"[:160]]
        results.append(CaseResult(id=case_id, passed=not problems, detail=";".join(problems)))

    failed = [c for c in results if not c.passed]
    skipped = sum(1 for c in results if c.skipped)
    notes = [f"通过 {len(results) - len(failed) - skipped}/{len(results) - skipped}"]
    if skipped:
        notes.append(f"{skipped} 条要嵌入模型的用例未跑(EMBED_* 未配置),不计分")
    return SuiteResult(
        name=path.stem,
        kind="calibration_probes",
        source=path.name,
        status="FAIL" if failed else "PASS",
        cases=results,
        notes=notes,
    )
