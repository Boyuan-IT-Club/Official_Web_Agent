"""周期复盘的状态面(包):指标快照表 + 跨表只读聚合。

- snapshot_store:cycle_metrics_snapshot 表,只追加、长期保留
- aggregates:按周期从对话日志、检索日志、评估三表里数出原始聚合,只读
"""

from __future__ import annotations

from official_agent.state.retrospective.aggregates import (
    conversation_aggregates,
    evaluation_usage_aggregates,
    kb_aggregates,
    qbank_aggregates,
    screening_aggregates,
)
from official_agent.state.retrospective.snapshot_store import (
    SnapshotRecord,
    content_hash,
    ensure_snapshot_table,
    get_snapshot,
    list_snapshots,
    reset_table_cache,
    save_snapshot,
)

__all__ = [
    "SnapshotRecord",
    "content_hash",
    "conversation_aggregates",
    "ensure_snapshot_table",
    "evaluation_usage_aggregates",
    "get_snapshot",
    "kb_aggregates",
    "list_snapshots",
    "qbank_aggregates",
    "reset_table_cache",
    "save_snapshot",
    "screening_aggregates",
]
