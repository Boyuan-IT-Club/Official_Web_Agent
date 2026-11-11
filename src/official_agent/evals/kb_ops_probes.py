"""知识库运营的确定性门禁执行器:聚类、近失判定、缺口提示、样本过滤。

数据集 evals/datasets/kb_ops_probes.yaml。全部是纯函数断言:聚类用数据集里写死的
向量,不调嵌入端点也不调模型,任何环境都能跑。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from official_agent.evals.engine import CaseResult, SuiteResult
from official_agent.kb_ops.cluster import (
    ClusterPolicy,
    ClusterResult,
    QueryRecord,
    build_topics,
    gap_hint,
    is_near_miss,
    normalize_query,
)
from official_agent.kb_ops.triage import looks_like_instruction


def _load_yaml(path: Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def env_blocker() -> str | None:
    return None


def _topic_shape(result: ClusterResult) -> list[dict[str, Any]]:
    """聚类结果 → 与数据集 expect.topics 同形的结构(只留断言关心的字段)。"""
    return [
        {
            "phrasings": [p.text for p in t.phrasings],
            "query_count": t.query_count,
            "thread_count": t.thread_count,
            "near_miss_count": t.near_miss_count,
            "gap_hint": gap_hint(t.near_miss_count, t.query_count),
        }
        for t in result.topics
    ]


def _clustering_cases(spec: dict[str, Any]) -> list[CaseResult]:
    policy = ClusterPolicy(**spec["policy"])
    records = [
        QueryRecord(
            id=int(q["id"]),
            thread_id=str(q["thread"]),
            query=str(q["text"]),
            best_vec_score=q.get("best"),
            created_at=datetime.fromisoformat(str(q["at"])),
        )
        for q in spec["queries"]
    ]
    vectors = {normalize_query(str(q["text"])): list(q["vector"]) for q in spec["queries"]}
    expect = spec["expect"]

    result = build_topics(records, vectors, policy)
    got = _topic_shape(result)
    counts = {"cluster_count": result.cluster_count, "below_support": result.below_support}
    want_counts = {k: expect[k] for k in counts}
    # 换一个输入顺序再跑:结果只应取决于 (created_at, id),不取决于传入的先后
    reordered = _topic_shape(build_topics(list(reversed(records)), vectors, policy))
    return [
        CaseResult(
            id="clustering_topics",
            passed=got == expect["topics"],
            detail="" if got == expect["topics"] else f"got={got}",
        ),
        CaseResult(
            id="clustering_counts",
            passed=counts == want_counts,
            detail="" if counts == want_counts else f"got={counts}",
        ),
        CaseResult(
            id="clustering_order_independent",
            passed=reordered == got,
            detail="" if reordered == got else f"reordered={reordered}",
        ),
    ]


def _value_cases(prefix: str, rows: list[dict[str, Any]], compute: Any) -> list[CaseResult]:
    """一组「输入 → 期望值」的断言。compute(row) 返回实际值,与 row['expect'] 比。"""
    cases = []
    for row in rows:
        got = compute(row)
        cases.append(
            CaseResult(
                id=f"{prefix}:{row['id']}",
                passed=got == row["expect"],
                detail="" if got == row["expect"] else f"got={got!r} expect={row['expect']!r}",
            )
        )
    return cases


async def run_suite(path: Path, *, distribution: bool = False, **_: Any) -> SuiteResult:
    data = _load_yaml(path) or {}
    cases = _clustering_cases(data["clustering"])
    cases += _value_cases(
        "near_miss",
        data.get("near_miss") or [],
        lambda row: is_near_miss(row.get("score"), float(row["floor"])),
    )
    cases += _value_cases(
        "gap_hint",
        data.get("gap_hint") or [],
        lambda row: gap_hint(int(row["near_miss"]), int(row["queries"])),
    )
    cases += _value_cases(
        "sample_filter",
        data.get("sample_filter") or [],
        lambda row: looks_like_instruction(str(row["text"])),
    )
    failed = [c.id for c in cases if not c.passed]
    return SuiteResult(
        name=path.stem,
        kind="kb_ops_probes",
        source=path.name,
        status="FAIL" if failed else "PASS",
        cases=cases,
        notes=[f"通过 {len(cases) - len(failed)}/{len(cases)}"],
    )
