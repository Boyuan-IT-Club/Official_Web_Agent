"""KB 召回门禁 executor(R7/#134 → #148 收口)。

自 feat/rag-kb 分支的 evals/run_kb_eval.py 迁入(彼分支无本引擎,合并时旧脚本
删除)。语义不变:golden probes 打 Recall@k / MRR 基线,负例 top1 不得过阈值;
只测外部行为(真实 embedding + 真实检索链),不 mock。

环境:kb.store 可导入 + EMBED_* + 语料已入库;缺任一 → SKIP(门禁不可静默
变绿)。kb.store/EMBED_* 源在 feat/rag-kb,本分支(feat/evaluation-b1)缺失,
executor 用可导入性探测,两分支合并前后行为都正确。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from official_agent.evals.engine import CaseResult, SuiteResult

#: search(path 注入)的形状:query × top_k → 命中列表(title/score 属性);
#: 签名放宽为 ...(真实 search 是 keyword-only top_k,fake 是位置参数)
SearchFn = Callable[..., Awaitable[list[Any]]]


def _load_yaml(path: Path) -> Any:
    """同步读盘抽小函数(ASYNC240:异步体内不做阻塞 IO)。"""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    """缺 kb.store 或 EMBED_* 都提前拦成 SKIP。"""
    try:
        import official_agent.kb.store  # noqa: F401
    except ImportError:
        return "kb.store 不在当前分支(feat/rag-kb 合并后可用)"
    from official_agent.config import get_settings

    embed_model = getattr(get_settings(), "embed_model", None)
    if not embed_model:
        return "EMBED_* 未配置(KB 召回门禁需要 embedding 服务)"
    return None


async def run_suite(
    path: Path, *, search: SearchFn | None = None, distribution: bool = False
) -> SuiteResult:
    """执行 kb_probes 数据集。distribution=True 仍算门禁结果,分布细节进 notes
    (调阈值用;CLI 侧分布模式强制退出码 0)。"""
    if search is None:
        try:
            from official_agent.kb.store import search as kb_search
        except ImportError as exc:
            return SuiteResult(
                name=path.stem,
                kind="kb_probes",
                source=path.name,
                status="SKIP",
                notes=[f"kb.store 不可导入({exc}),本分支不跑 KB 门禁"],
            )
        search = kb_search

    data = _load_yaml(path)
    defaults = data.get("defaults", {})
    probes = data.get("probes", [])
    top_k = int(defaults.get("top_k", 3))
    neg_max = float(defaults.get("negative_max_score", 0.50))
    min_recall = float(defaults.get("min_recall_at_3", 0.95))
    min_mrr = float(defaults.get("min_mrr", 0.75))

    positives = [p for p in probes if p.get("kind", "positive") == "positive"]
    negatives = [p for p in probes if p.get("kind") == "negative"]

    cases: list[CaseResult] = []
    golden_scores: list[float] = []
    neg_scores: list[float] = []
    reciprocal = 0.0
    for probe in positives:
        hits = await search(probe["query"], top_k=top_k)
        titles = [h.title for h in hits]
        expected = probe.get("expect", {}).get("any_of_sources", [])
        rank = next((i + 1 for i, t in enumerate(titles) if t in expected), None)
        top1 = hits[0].score if hits else 0.0
        golden_scores.append(top1)
        if rank:
            reciprocal += 1 / rank
            cases.append(CaseResult(id=probe["id"], passed=True, detail=f"rank={rank}"))
        else:
            cases.append(
                CaseResult(
                    id=probe["id"],
                    passed=False,
                    detail=f"top={titles} expect={expected} top1={top1:.3f}",
                )
            )

    for probe in negatives:
        hits = await search(probe["query"], top_k=1)
        score = hits[0].score if hits else 0.0
        neg_scores.append(score)
        cases.append(
            CaseResult(
                id=probe["id"],
                passed=score < neg_max,
                detail=f"top1={score:.3f} (阈值 {neg_max})",
            )
        )

    n_pos = len(positives)
    recall = sum(1 for c in cases[:n_pos] if c.passed) / n_pos if n_pos else 1.0
    mrr = reciprocal / n_pos if n_pos else 1.0

    notes = []
    if golden_scores:
        notes.append(f"golden top1 分布: min={min(golden_scores):.3f} max={max(golden_scores):.3f}")
    if neg_scores:
        notes.append(
            f"negative top1 分布: min={min(neg_scores):.3f} max={max(neg_scores):.3f}"
            f" | 阈值 {neg_max}"
        )

    metrics = {f"recall_at_{top_k}": recall, "mrr": mrr}
    threshold_breach = recall < min_recall or mrr < min_mrr
    any_case_fail = any(not c.passed for c in cases)
    return SuiteResult(
        name=path.stem,
        kind="kb_probes",
        source=path.name,
        status="FAIL" if any_case_fail or threshold_breach else "PASS",
        cases=cases,
        metrics=metrics,
        notes=notes,
    )
