"""评审队列投影:每简历最新卡 + 归属人 + 历史决策,跨表 JOIN 只读。"""

from __future__ import annotations

from typing import Any

from official_agent.state.evaluation import _connection, bootstrap
from official_agent.state.evaluation.job_store import _MAX_PAGE

_QUEUE_KEY = "scorecard"  # 队列依赖 scorecard 表;与 scorecard_store 共用自举


def _ensure_bootstrapped() -> None:
    bootstrap.ensure_once(_QUEUE_KEY, _bootstrap)


def _bootstrap() -> None:
    from official_agent.state.evaluation.scorecard_store import ensure_evaluation_tables

    with _connection._conn() as conn:
        ensure_evaluation_tables(conn)


def list_review_queue(
    cycle_id: int,
    queue: str = "all",
    *,
    limit: int = _MAX_PAGE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """评审队列投影:每简历最新卡 + 关联 user_id(勾选重评需要)。

    queue=zero → 仅初筛不过(hard_zero)子队列;all → 全部。

    decided_status/decided_version:该简历在本周期**历史上**最后一次人工
    决策(adopted/rejected)落在哪一版。复评会写一张新的 draft 卡,
    `status` 随之回到 draft;没有这对字段,评审人分不清"还没看过"和
    "看过、已采纳、只是又重跑了一遍"。禁止复评会丢掉能力(复评本就合法),
    所以在投影里把决策历史带出来,由前端区分呈现。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        outer = "WHERE latest.hard_zero = TRUE" if queue == "zero" else ""
        rows = conn.execute(
            f"""
            SELECT latest.resume_id, latest.card_version, latest.status,
                   latest.hard_zero, latest.total, latest.prompt_version,
                   latest.created_at, j.user_id,
                   decided.status AS decided_status,
                   decided.card_version AS decided_version
            FROM (
                SELECT DISTINCT ON (s.resume_id)
                    s.resume_id, s.card_version, s.status, s.hard_zero, s.total,
                    s.prompt_version, s.created_at
                FROM evaluation_scorecard s WHERE s.cycle_id = %s
                ORDER BY s.resume_id, s.card_version DESC
            ) latest
            LEFT JOIN LATERAL (
                SELECT j2.user_id FROM evaluation_job j2
                WHERE j2.resume_id = latest.resume_id AND j2.cycle_id = %s
                ORDER BY j2.job_id DESC LIMIT 1
            ) j ON TRUE
            LEFT JOIN LATERAL (
                SELECT d.status, d.card_version FROM evaluation_scorecard d
                WHERE d.resume_id = latest.resume_id AND d.cycle_id = %s
                  AND d.status IN ('adopted', 'rejected')
                ORDER BY d.card_version DESC LIMIT 1
            ) decided ON TRUE
            {outer}
            ORDER BY latest.resume_id
            LIMIT %s OFFSET %s
            """,  # noqa: S608
            (
                cycle_id,
                cycle_id,
                cycle_id,
                max(1, min(limit, _MAX_PAGE)),
                max(0, offset),
            ),
        ).fetchall()
        return [dict(r) for r in rows]
