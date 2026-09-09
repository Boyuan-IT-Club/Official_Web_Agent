"""RAG KB 召回门禁(R7,#134):golden probes → Recall@3 / MRR / 负例拒绝。

- 只测外部行为:真实 embedding + 真实检索链(kb.store.search),不 mock
- 需要环境:Agent PG 可用(.env POSTGRES_URL)+ EMBED_* 配置 + 语料已入库
  (面板/脚本录入);CI 无环境时跳过
- 退出码非 0 = 挡合并:Recall@3 / MRR 低于基线,或任一负例误命中

用法:
  uv run python evals/run_kb_eval.py            # 执行门禁
  uv run python evals/run_kb_eval.py --distribution  # 打分分布(调阈值用)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

from official_agent.config import get_settings
from official_agent.kb.embedding import EmbeddingNotConfiguredError
from official_agent.kb.store import search

_DATASET = Path(__file__).parent / "datasets" / "kb_probes.yaml"


def load_dataset() -> tuple[dict, list[dict]]:
    data = yaml.safe_load(_DATASET.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    return defaults, data.get("probes", [])


async def run(*, distribution: bool = False) -> int:
    defaults, probes = load_dataset()
    top_k = int(defaults.get("top_k", 3))
    neg_max = float(defaults.get("negative_max_score", 0.50))
    min_recall = float(defaults.get("min_recall_at_3", 0.95))
    min_mrr = float(defaults.get("min_mrr", 0.75))

    positives = [p for p in probes if p.get("kind", "positive") == "positive"]
    negatives = [p for p in probes if p.get("kind") == "negative"]

    hit = 0
    reciprocal = 0.0
    golden_scores: list[float] = []
    misses: list[str] = []
    for probe in positives:
        hits_list = await search(probe["query"], top_k=top_k)
        titles = [h.title for h in hits_list]
        expected = probe.get("expect", {}).get("any_of_sources", [])
        rank = next(
            (i + 1 for i, t in enumerate(titles) if t in expected), None
        )
        top1 = hits_list[0].score if hits_list else 0.0
        golden_scores.append(top1)
        if rank:
            hit += 1
            reciprocal += 1 / rank
        else:
            misses.append(
                f"{probe['id']}: top={titles} expect={expected} top1={top1:.3f}"
            )

    neg_hits: list[str] = []
    neg_scores: list[float] = []
    for probe in negatives:
        hits_list = await search(probe["query"], top_k=1)
        score = hits_list[0].score if hits_list else 0.0
        neg_scores.append(score)
        if score >= neg_max:
            neg_hits.append(f"{probe['id']}: top1={score:.3f} ≥ {neg_max}")

    recall_at_k = hit / len(positives) if positives else 1.0
    mrr = reciprocal / len(positives) if positives else 1.0

    print(f"probes: positive={len(positives)} negative={len(negatives)}")
    print(
        f"Recall@{top_k} = {recall_at_k:.3f} (基线 ≥ {min_recall}) | "
        f"MRR = {mrr:.3f} (基线 ≥ {min_mrr})"
    )
    print(
        f"golden top1 分布: min={min(golden_scores):.3f} "
        f"max={max(golden_scores):.3f}" if golden_scores else "no golden"
    )
    if neg_scores:
        print(
            f"negative top1 分布: min={min(neg_scores):.3f} "
            f"max={max(neg_scores):.3f} | 阈值 {neg_max}"
        )
    for m in misses:
        print("MISS:", m)
    for h in neg_hits:
        print("FALSE-HIT:", h)

    if distribution:
        return 0

    failed = bool(misses) or bool(neg_hits) or recall_at_k < min_recall or mrr < min_mrr
    print("GATE:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


def main() -> int:
    settings = get_settings()
    if not settings.embed_model:
        print("EMBED_* 未配置:KB 召回门禁无法运行(检查点①)")
        return 2
    distribution = "--distribution" in sys.argv
    try:
        return asyncio.run(run(distribution=distribution))
    except EmbeddingNotConfiguredError as exc:
        print("EMBED_* 未配置:", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
