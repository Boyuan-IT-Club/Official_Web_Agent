"""evaluation_scorecard 数据面:AI 参考分卡,版本递增旧版保留。

- AI 只作参考:卡存 Agent PG,**不写** resume_score_entry / resume_score
  (后端多人打分是真人票)
- 重跑版本递增:UNIQUE (resume_id, cycle_id, card_version),旧版可回看
- 卡态:draft(默认)→ adopted/rejected(由评审面迁移)
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from official_agent.state.evaluation import _connection, bootstrap

_STATUS_DRAFT = "draft"

_BOOTSTRAP_KEY = "scorecard"


def _ensure_bootstrapped() -> None:
    bootstrap.ensure_once(_BOOTSTRAP_KEY, _bootstrap)


def _bootstrap() -> None:
    with _connection._conn() as conn:
        ensure_evaluation_tables(conn)


def ensure_evaluation_tables(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """幂等建 evaluation_scorecard(DDL 进仓库,新环境自举;仅启动调用)。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_scorecard (
            id             bigserial   NOT NULL PRIMARY KEY,
            resume_id      bigint      NOT NULL,
            cycle_id      int         NOT NULL,
            card_version   int         NOT NULL,
            status         text        NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft', 'adopted', 'rejected')),
            hard_zero      boolean     NOT NULL DEFAULT false,
            total          real,
            card           jsonb       NOT NULL,
            prompt_version text        NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now(),
            UNIQUE (resume_id, cycle_id, card_version)
        )
        """
    )
    # 评审队列按周期取"每简历最新版":前导列必须是 cycle_id,
    # 且 (resume_id asc, card_version desc) 与 DISTINCT ON 的排序一致。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_scorecard_cycle "
        "ON evaluation_scorecard (cycle_id, resume_id, card_version DESC)"
    )
    # (resume_id, cycle_id) 是 UNIQUE(resume_id, cycle_id, card_version) 的严格前缀
    conn.execute("DROP INDEX IF EXISTS idx_eval_scorecard_resume")


def ensure_evaluation_scorecard_ready() -> None:
    """启动自举:建 evaluation_scorecard(lifespan 调用);重复调用无害
    (DDL 全 IF NOT EXISTS),并置位数据路径守卫。"""
    with _connection._conn() as conn:
        ensure_evaluation_tables(conn)
    bootstrap.mark_done(_BOOTSTRAP_KEY)


def save_scorecard(
    card: dict[str, Any],
    *,
    resume_id: int,
    cycle_id: int,
    prompt_version: str,
) -> int:
    """落卡:版本 = 该 (resume, cycle) 现存最大版本+1(重跑递增,旧版保留)。

    hard_zero/total 从卡内冗余提列,供队列过滤(0 分队列)与列表免解 JSONB。
    返回本次 card_version。
    """
    _ensure_bootstrapped()
    # MAX+1 读改写有并发窗口(同简历并发重触发):撞唯一键重读重试
    for attempt in range(2):
        try:
            with _connection._conn() as conn:
                row = conn.execute(
                    "SELECT COALESCE(MAX(card_version), 0) AS v "
                    "FROM evaluation_scorecard WHERE resume_id = %s AND cycle_id = %s",
                    (resume_id, cycle_id),
                ).fetchone()
                version = (row["v"] if row else 0) + 1
                conn.execute(
                    """
                    INSERT INTO evaluation_scorecard
                        (resume_id, cycle_id, card_version, status, hard_zero, total,
                         card, prompt_version)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resume_id,
                        cycle_id,
                        version,
                        _STATUS_DRAFT,
                        bool(card.get("hard_zero", False)),
                        card.get("total"),
                        # 卡 JSONB(ensure_ascii=False,评审面板直读中文)
                        json.dumps(card, ensure_ascii=False),
                        prompt_version,
                    ),
                )
            return version
        except psycopg.errors.UniqueViolation:
            if attempt:
                raise
    raise RuntimeError("unreachable")


def cycle_scoring_mode(cycle_id: int) -> str | None:
    """本周期第一张评分卡所用的评分模式;周期里还没有卡 → None。

    一个周期只用一种评分模式,以最早落库的那张卡为准。没有 panel 块的卡出自
    单评委图,记为 single。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        row = conn.execute(
            "SELECT card->'panel'->>'mode' AS mode FROM evaluation_scorecard "
            "WHERE cycle_id = %s ORDER BY id LIMIT 1",
            (cycle_id,),
        ).fetchone()
    if row is None:
        return None
    return row["mode"] or "single"


def cycle_calibration_set(cycle_id: int) -> int | None:
    """本周期冻结的校准案例集水位;周期里还没有卡 → None。

    与评分模式同理,以最早落库的那张卡为准:它记了水位就沿用,没记(单评委模式的卡,
    或校准案例启用之前出的卡)就是 0——本周期不用校准案例。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE((card->'panel'->'calibration'->>'set')::int, 0) AS watermark "
            "FROM evaluation_scorecard WHERE cycle_id = %s ORDER BY id LIMIT 1",
            (cycle_id,),
        ).fetchone()
    return int(row["watermark"]) if row else None


def latest_scorecard(resume_id: int, cycle_id: int) -> dict[str, Any] | None:
    """最新一版的完整卡;无则 None。"""
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        row = conn.execute(
            "SELECT card, card_version, status, hard_zero, total, prompt_version, created_at "
            "FROM evaluation_scorecard "
            "WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY card_version DESC LIMIT 1",
            (resume_id, cycle_id),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if isinstance(result["card"], str):
        result["card"] = json.loads(result["card"])
    return result


def list_scorecards(resume_id: int, cycle_id: int) -> list[dict[str, Any]]:
    """全部版本投影(不含 card JSONB,详情走 latest/get)。"""
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        rows = conn.execute(
            "SELECT card_version, status, hard_zero, total, prompt_version, created_at "
            "FROM evaluation_scorecard WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY card_version DESC",
            (resume_id, cycle_id),
        ).fetchall()
    return [dict(r) for r in rows]


def set_scorecard_status(resume_id: int, cycle_id: int, version: int, status: str) -> bool:
    """卡态迁移(draft→adopted/rejected;评审采纳/驳回写这里)。"""
    if status not in ("draft", "adopted", "rejected"):
        raise ValueError(f"非法卡态:{status!r}")
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        cur = conn.execute(
            "UPDATE evaluation_scorecard SET status = %s "
            "WHERE resume_id = %s AND cycle_id = %s AND card_version = %s",
            (status, resume_id, cycle_id, version),
        )
        return cur.rowcount > 0
