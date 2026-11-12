"""evaluation_calibration_case 数据面:初筛的校准案例(往届判例),条目级。

- 一行 =(清单, 判定项, 达成与否, 引文, 理由)+ 这段文本的向量。不存整份简历
- 案例不可改:录错了就停用(retire)再录一条。停用立即生效;新增的案例只对
  「之后才开跑的周期」生效(按周期冻结的水位见 anchors.eligible_cases)
- 向量存成 real[] 而不是 pgvector 的 vector:检索是按判定项过滤后在代码里算相似度,
  几十到几百条的量级用不着向量索引,这张表的自举也就不依赖 pgvector 扩展。
  每行记 embed_model:不同模型算出的向量不可比,读取时只取当前模型的行
"""

from __future__ import annotations

from typing import Any

import psycopg

from official_agent.state.evaluation import _connection, bootstrap
from official_agent.state.evaluation.job_store import _MAX_PAGE

_BOOTSTRAP_KEY = "calibration"

#: 列表投影:不带向量(一条向量上千个浮点数,管理面用不着)
_PUBLIC_COLUMNS = (
    "case_id, origin, source_cycle_id, source_resume_id, checklist, item, met, "
    "quote, reason, embed_model, created_by, created_at, retired_at"
)


def _ensure_bootstrapped() -> None:
    bootstrap.ensure_once(_BOOTSTRAP_KEY, _bootstrap)


def _bootstrap() -> None:
    with _connection._conn() as conn:
        ensure_calibration_table(conn)


def ensure_calibration_table(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """幂等建 evaluation_calibration_case(每进程首次使用时执行一次)。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_calibration_case (
            case_id          bigserial   NOT NULL PRIMARY KEY,
            origin           text        NOT NULL CHECK (origin IN ('seed', 'adopted_card')),
            source_cycle_id  int,
            source_resume_id bigint,
            checklist        text        NOT NULL,
            item             text        NOT NULL,
            met              boolean     NOT NULL,
            quote            text        NOT NULL DEFAULT '',
            reason           text        NOT NULL,
            embedding        real[]      NOT NULL,
            embed_model      text        NOT NULL,
            created_by       bigint,
            created_at       timestamptz NOT NULL DEFAULT now(),
            retired_at       timestamptz
        )
        """
    )
    # 同一张卡的同一个判定项只入库一次:重复收割同一个周期不产生重复案例
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_calibration_source "
        "ON evaluation_calibration_case (source_cycle_id, source_resume_id, checklist, item) "
        "WHERE origin = 'adopted_card'"
    )


def add_case(
    case: dict[str, Any],
    *,
    origin: str,
    embedding: list[float],
    embed_model: str,
    source_cycle_id: int | None = None,
    source_resume_id: int | None = None,
    created_by: int | None = None,
) -> int | None:
    """写入一条案例,返回案例号;同一张卡的同一个判定项已经入过库则返回 None。

    case 是已经过入库筛查的字段(anchors.screen_case 的返回值):本函数不再检查内容。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        row = conn.execute(
            """
            INSERT INTO evaluation_calibration_case
                (origin, source_cycle_id, source_resume_id, checklist, item, met,
                 quote, reason, embedding, embed_model, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING case_id
            """,
            (
                origin,
                source_cycle_id,
                source_resume_id,
                case["checklist"],
                case["item"],
                case["met"],
                case["quote"],
                case["reason"],
                embedding,
                embed_model,
                created_by,
            ),
        ).fetchone()
    return int(row["case_id"]) if row else None


def retire_case(case_id: int) -> bool:
    """停用一条案例(不删行,留着追溯);返回是否有一条在用的案例被停用。"""
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        cur = conn.execute(
            "UPDATE evaluation_calibration_case SET retired_at = now() "
            "WHERE case_id = %s AND retired_at IS NULL",
            (case_id,),
        )
        return cur.rowcount > 0


def list_cases(
    *,
    checklist: str | None = None,
    item: str | None = None,
    include_retired: bool = False,
    limit: int = _MAX_PAGE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """案例列表(不带向量),按案例号升序;可按清单、判定项过滤。"""
    _ensure_bootstrapped()
    conds: list[str] = []
    params: list[Any] = []
    if checklist is not None:
        conds.append("checklist = %s")
        params.append(checklist)
    if item is not None:
        conds.append("item = %s")
        params.append(item)
    if not include_retired:
        conds.append("retired_at IS NULL")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    with _connection._conn() as conn:
        rows = conn.execute(
            f"SELECT {_PUBLIC_COLUMNS} FROM evaluation_calibration_case {where} "  # noqa: S608
            "ORDER BY case_id LIMIT %s OFFSET %s",
            (*params, max(1, min(limit, _MAX_PAGE)), max(0, offset)),
        ).fetchall()
    return [dict(r) for r in rows]


def current_watermark() -> int:
    """当前最大的案例号;一条案例都没有时为 0。新周期开跑时把它定为本周期的水位。"""
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(case_id), 0) AS watermark FROM evaluation_calibration_case"
        ).fetchone()
    return int(row["watermark"]) if row else 0


def load_cases_for_retrieval(embed_model: str) -> list[dict[str, Any]]:
    """检索用:全部在用、且向量出自 embed_model 的案例,带向量。

    只按「在用」和「同一个嵌入模型」过滤;水位、周期、简历的隔离在代码里做
    (anchors.eligible_cases),那几条规则要能脱离数据库单独验收。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        rows = conn.execute(
            "SELECT case_id, source_cycle_id, source_resume_id, checklist, item, met, "
            "quote, reason, embedding FROM evaluation_calibration_case "
            "WHERE retired_at IS NULL AND embed_model = %s ORDER BY case_id",
            (embed_model,),
        ).fetchall()
    return [dict(r) for r in rows]


def adopted_human_scores(cycle_id: int) -> dict[int, int]:
    """本周期每份被采纳的简历,评审人采纳时给的分数(0-100);同一份简历取最后一次。

    分数只在采纳的审计记录里有(评审人以本人身份向后端投的那一票),所以读审计表。
    """
    with _connection._conn() as conn:
        rows = conn.execute(
            """
            SELECT (action->>'resume_id')::bigint AS resume_id,
                   (action->>'score')::int AS score
            FROM agent_audit_log
            WHERE action->>'op' = 'adopt_scorecard'
              AND (action->>'cycle_id')::int = %s
              AND action->>'score' IS NOT NULL
            ORDER BY id
            """,
            (cycle_id,),
        ).fetchall()
    return {int(r["resume_id"]): int(r["score"]) for r in rows}


def list_adopted_cards(cycle_id: int) -> list[dict[str, Any]]:
    """本周期每份简历的最新一版卡里,已被采纳的那些(带整张卡)。"""
    with _connection._conn() as conn:
        rows = conn.execute(
            """
            SELECT latest.resume_id, latest.card_version, latest.card
            FROM (
                SELECT DISTINCT ON (s.resume_id) s.resume_id, s.card_version, s.status, s.card
                FROM evaluation_scorecard s WHERE s.cycle_id = %s
                ORDER BY s.resume_id, s.card_version DESC
            ) latest
            WHERE latest.status = 'adopted'
            ORDER BY latest.resume_id
            """,
            (cycle_id,),
        ).fetchall()
    return [dict(r) for r in rows]
