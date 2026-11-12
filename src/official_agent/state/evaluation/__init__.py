"""evaluation 状态面(包):scorecard / job / 评审队列 + 自举与连接。

拆分自单文件版;本 `__init__` re-export 全部公共名,消费方
(`from official_agent.state import evaluation`)零改动。校准案例的数据面在
calibration_store 模块,按模块名引用(`evaluation.calibration_store`)。
"""

from __future__ import annotations

from official_agent.state.evaluation import calibration_store
from official_agent.state.evaluation._connection import _conn, get_pool, reset_pool
from official_agent.state.evaluation.bootstrap import ensure_once
from official_agent.state.evaluation.job_store import (
    _MAX_ATTEMPTS,
    _MAX_PAGE,
    JobStoreError,
    claim_job,
    claim_next_pending,
    create_jobs,
    ensure_evaluation_job_integrity,
    ensure_evaluation_job_ready,
    ensure_evaluation_job_table,
    get_job,
    latest_job,
    list_jobs,
    mark_job,
    release_claim,
    requeue_failed,
    requeue_stale,
    requeue_stale_all_cycles,
)
from official_agent.state.evaluation.review_queue import list_pool_entries, list_review_queue
from official_agent.state.evaluation.scorecard_store import (
    cycle_calibration_set,
    cycle_scoring_mode,
    ensure_evaluation_scorecard_ready,
    ensure_evaluation_tables,
    latest_scorecard,
    list_scorecards,
    save_scorecard,
    set_scorecard_status,
)

__all__ = [
    "_MAX_ATTEMPTS",
    "_MAX_PAGE",
    "_conn",
    "JobStoreError",
    "calibration_store",
    "claim_job",
    "claim_next_pending",
    "create_jobs",
    "cycle_calibration_set",
    "cycle_scoring_mode",
    "ensure_evaluation_job_integrity",
    "ensure_evaluation_job_ready",
    "ensure_evaluation_job_table",
    "ensure_evaluation_scorecard_ready",
    "ensure_evaluation_tables",
    "ensure_once",
    "get_job",
    "get_pool",
    "latest_job",
    "latest_scorecard",
    "list_jobs",
    "list_pool_entries",
    "list_review_queue",
    "list_scorecards",
    "mark_job",
    "reset_pool",
    "requeue_failed",
    "requeue_stale",
    "release_claim",
    "requeue_stale_all_cycles",
    "save_scorecard",
    "set_scorecard_status",
]
