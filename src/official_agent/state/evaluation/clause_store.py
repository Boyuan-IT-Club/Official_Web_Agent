"""evaluation_clause_candidate 数据面:评分条款的候选,按周期存。

候选是周期结束后从人机不一致的判定里归纳出来的建议,只给人看:评分路径不读这张表,
也没有任何代码据此改 prompt。每次为一个周期重新归纳,整批替换该周期原有的候选。

周期复盘报告从这里取候选:只读函数 list_candidates。
"""

from __future__ import annotations

from typing import Any

import psycopg

from official_agent.state.evaluation import _connection, bootstrap

_BOOTSTRAP_KEY = "clause_candidate"


def _ensure_bootstrapped() -> None:
    bootstrap.ensure_once(_BOOTSTRAP_KEY, _bootstrap)


def _bootstrap() -> None:
    with _connection._conn() as conn:
        ensure_clause_table(conn)


def ensure_clause_table(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """幂等建 evaluation_clause_candidate(每进程首次使用时执行一次)。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_clause_candidate (
            candidate_id     bigserial   NOT NULL PRIMARY KEY,
            cycle_id         int         NOT NULL,
            checklist        text        NOT NULL,
            item             text        NOT NULL,
            direction        text        NOT NULL CHECK (direction IN ('lenient', 'strict')),
            clause           text        NOT NULL,
            supporting_cards int         NOT NULL,
            prompt_version   text        NOT NULL,
            model            text        NOT NULL,
            created_by       bigint,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_clause_candidate_cycle "
        "ON evaluation_clause_candidate (cycle_id)"
    )


def replace_candidates(
    cycle_id: int,
    candidates: list[dict[str, Any]],
    *,
    prompt_version: str,
    model: str,
    created_by: int | None,
) -> int:
    """把一个周期的候选整批换成这一次归纳的结果,返回写入的条数。

    删旧与写新在同一个事务里:要么看到旧的一批,要么看到新的一批。candidates 为空
    即清空该周期的候选(这次没有归纳出任何东西)。每项的键:checklist、item、
    direction、clause、supporting_cards;文字须已过入库前的闸(clauses.screen_clause)。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        conn.execute("DELETE FROM evaluation_clause_candidate WHERE cycle_id = %s", (cycle_id,))
        for candidate in candidates:
            conn.execute(
                """
                INSERT INTO evaluation_clause_candidate
                    (cycle_id, checklist, item, direction, clause, supporting_cards,
                     prompt_version, model, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cycle_id,
                    candidate["checklist"],
                    candidate["item"],
                    candidate["direction"],
                    candidate["clause"],
                    candidate["supporting_cards"],
                    prompt_version,
                    model,
                    created_by,
                ),
            )
    return len(candidates)


def list_candidates(cycle_id: int) -> list[dict[str, Any]]:
    """一个周期已生成的条款候选,支持卡数多的在前;没有生成过返回空列表。只读。

    每项的键:candidate_id、cycle_id、checklist(部门名或「认真程度」)、item(判定项名)、
    direction(lenient = AI 判得比人宽 / strict = AI 判得比人严)、clause(候选文字,已过
    长度、个人信息、指令样文本三道闸)、supporting_cards(在这个判定项上人机不一致的
    卡数)、prompt_version、model、created_by、created_at。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        rows = conn.execute(
            "SELECT candidate_id, cycle_id, checklist, item, direction, clause, "
            "supporting_cards, prompt_version, model, created_by, created_at "
            "FROM evaluation_clause_candidate WHERE cycle_id = %s "
            "ORDER BY supporting_cards DESC, candidate_id",
            (cycle_id,),
        ).fetchall()
    return [dict(r) for r in rows]
