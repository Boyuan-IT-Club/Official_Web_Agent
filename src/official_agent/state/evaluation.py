"""evaluation_scorecard 数据面(B1):AI 参考分卡,版本递增旧版保留(#124)。

- AI 只作参考:卡存 Agent PG,**不写** resume_score_entry / resume_score
  (后端多人打分是真人票,#216 对齐)
- 重跑版本递增:UNIQUE (resume_id, cycle_id, card_version),旧版可回看
- 卡态:draft(默认)→ adopted/rejected(B6 评审队列迁移)
- job 面(B2):幂等创建(闸门2)+ 启动自动恢复(闸门3)+ qbank 独立状态(闸门6)

表自举 L-1 先例:DDL 进仓库,幂等;调用方管理事务(threads.py 风格)。
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings

_STATUS_DRAFT = "draft"
# job 自动重试上限(评审闸门3):对齐 tools/client 的 _MAX_ATTEMPTS;
# 超过即标 failed 并停止自动重排,交人工队列。
_MAX_ATTEMPTS = 3


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_evaluation_tables(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """幂等建 evaluation_scorecard(L-1:新环境自举; lifespan 调用,B2 接线)。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_scorecard (
            id             bigserial   NOT NULL PRIMARY KEY,
            resume_id      bigint      NOT NULL,
            cycle_id       int         NOT NULL,
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_scorecard_resume "
        "ON evaluation_scorecard (resume_id, cycle_id)"
    )


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
    # MAX+1 读改写有并发窗口(同简历并发重触发,B2 评审 P2):撞唯一键重读重试
    for attempt in range(2):
        try:
            with _conn() as conn:
                ensure_evaluation_tables(conn)
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


def latest_scorecard(resume_id: int, cycle_id: int) -> dict[str, Any] | None:
    """最新一版的完整卡;无则 None。"""
    with _conn() as conn:
        ensure_evaluation_tables(conn)
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
    with _conn() as conn:
        ensure_evaluation_tables(conn)
        rows = conn.execute(
            "SELECT card_version, status, hard_zero, total, prompt_version, created_at "
            "FROM evaluation_scorecard WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY card_version DESC",
            (resume_id, cycle_id),
        ).fetchall()
    return [dict(r) for r in rows]


def set_scorecard_status(resume_id: int, cycle_id: int, version: int, status: str) -> bool:
    """卡态迁移(draft→adopted/rejected;B6 评审采纳/驳回写这里)。"""
    if status not in ("draft", "adopted", "rejected"):
        raise ValueError(f"非法卡态:{status!r}")
    with _conn() as conn:
        ensure_evaluation_tables(conn)
        cur = conn.execute(
            "UPDATE evaluation_scorecard SET status = %s "
            "WHERE resume_id = %s AND cycle_id = %s AND card_version = %s",
            (status, resume_id, cycle_id, version),
        )
        return cur.rowcount > 0


# ── B2 执行组织(#126):job 状态表 + 进程内 runner 的持久态 ────────────────


def ensure_evaluation_job_table(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """幂等建 evaluation_job;与 scorecard 同库同自举纪律。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evaluation_job (
            job_id      bigserial   NOT NULL PRIMARY KEY,
            resume_id   bigint      NOT NULL,
            user_id     bigint      NOT NULL,
            cycle_id    int         NOT NULL,
            status      text        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
            attempts    int         NOT NULL DEFAULT 0,
            error       text,
            card_version int,
            qbank_status text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # 旧库自举:qbank_status 列对已存在表补加(闸门6)
    conn.execute("ALTER TABLE evaluation_job ADD COLUMN IF NOT EXISTS qbank_status text")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_eval_job_active_updated
        ON evaluation_job (updated_at)
        WHERE status IN ('pending', 'running')
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_job_cycle ON evaluation_job (cycle_id, status)"
    )


def ensure_evaluation_job_integrity(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """闸门2 加固(启动时调用一次):去重活跃 job + 建部分唯一索引。

    与 ensure_evaluation_job_table 分开的原因:
    - 去重是 O(表) 的 UPDATE,不该每次 DB 操作都跑;
    - CREATE UNIQUE INDEX 在旧库存在重复活跃 job 时会直接失败,必须先去重;
    - create_jobs 自身用 SELECT-first + savepoint 保证正确性,
      本索引只是防御性兜底(并发竞态的最后一道闸)。
    """
    # 保留每 (resume_id, cycle_id) 最新的活跃 job,其余落 failed
    conn.execute(
        """
        UPDATE evaluation_job SET status = 'failed',
            error = COALESCE(error, '') || ' [启动去重:同简历存在更新的活跃 job]',
            updated_at = now()
        WHERE status IN ('pending', 'running')
          AND job_id NOT IN (
              SELECT MAX(job_id) FROM evaluation_job
              WHERE status IN ('pending', 'running')
              GROUP BY resume_id, cycle_id
          )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_job_active_resume
        ON evaluation_job (resume_id, cycle_id)
        WHERE status IN ('pending', 'running')
        """
    )


def ensure_evaluation_job_ready() -> None:
    """启动自举(#175 评审):建表 + 去重 legacy 重复活跃 job + 建唯一索引。

    requeue 守卫只防"恢复时撞索引";重复活跃行的消除靠这里。lifespan 在
    启动恢复**之前**调用,保证恢复面对的是去重后的活跃集。
    """
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        ensure_evaluation_job_integrity(conn)


def create_jobs(items: list[tuple[int, int]], cycle_id: int) -> list[int]:
    """每份简历一行 job;幂等(闸门2):同 (resume, cycle) 已有活跃 job → 返回已有 job_id。

    终态(succeeded/failed)不拦——复评是合法操作(新版本卡);活跃才去重。
    SELECT-first:先查活跃 job,有则直接复用;无则 INSERT。INSERT 包在
    savepoint 里,并发撞唯一键(uq_eval_job_active_resume)时回滚 savepoint
    并读回已有 job,不让事务进入 aborted 状态。
    """
    ids: list[int] = []
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        for resume_id, user_id in items:
            existing = _find_active_job(conn, resume_id, cycle_id)
            if existing is not None:
                ids.append(existing)
                continue
            try:
                with conn.transaction():
                    row = conn.execute(
                        """
                        INSERT INTO evaluation_job (resume_id, user_id, cycle_id)
                        VALUES (%s, %s, %s) RETURNING job_id
                        """,
                        (resume_id, user_id, cycle_id),
                    ).fetchone()
                if row:
                    ids.append(int(row["job_id"]))
                    continue
            except psycopg.errors.UniqueViolation:
                pass  # 并发撞唯一键 → 读回已有活跃 job
            existing = _find_active_job(conn, resume_id, cycle_id)
            if existing is None:
                raise RuntimeError(
                    f"创建 job 失败且无活跃 job 可复用(resume={resume_id}, cycle={cycle_id})"
                )
            ids.append(existing)
    return ids


def _find_active_job(
    conn: psycopg.Connection[dict[str, Any]], resume_id: int, cycle_id: int
) -> int | None:
    """该 (resume, cycle) 最新一条活跃 job_id;无则 None。"""
    row = conn.execute(
        "SELECT job_id FROM evaluation_job "
        "WHERE resume_id = %s AND cycle_id = %s "
        "AND status IN ('pending', 'running') "
        "ORDER BY job_id DESC LIMIT 1",
        (resume_id, cycle_id),
    ).fetchone()
    return int(row["job_id"]) if row else None


def mark_job(
    job_id: int,
    status: str,
    *,
    error: str | None = None,
    card_version: int | None = None,
    qbank_status: str | None = None,
) -> bool:
    """状态迁移(pending→running→succeeded/failed;failed 可重试回 pending)。

    qbank_status(闸门6):题库线独立完成态——succeeded/failed/skipped。
    job 终态 succeeded 但 qbank_status=failed 时,管理面可见"有评分无题库"。
    """
    if status not in ("pending", "running", "succeeded", "failed"):
        raise ValueError(f"非法 job 状态:{status!r}")
    # attempts 语义=实际执行次数:只在进入 running 时累加(B2 评审 P2)
    bump = ", attempts = attempts + 1" if status == "running" else ""
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        cur = conn.execute(
            f"""
            UPDATE evaluation_job
            SET status = %s, error = %s, card_version = %s,
                qbank_status = %s,
                updated_at = now(){bump}
            WHERE job_id = %s
            """,
            (status, error, card_version, qbank_status, job_id),
        )
        return cur.rowcount > 0


def get_job(job_id: int) -> dict[str, Any] | None:
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        row = conn.execute(
            "SELECT job_id, resume_id, user_id, cycle_id, status, attempts, error, "
            "card_version, qbank_status, created_at, updated_at "
            "FROM evaluation_job WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def list_jobs(cycle_id: int, *, status: str | None = None) -> list[dict[str, Any]]:
    """按周期查 job(0 分队列在 B6 按 scorecard.hard_zero 过滤,这里看执行面)。"""
    where = "cycle_id = %s"
    params: list[Any] = [cycle_id]
    if status:
        where += " AND status = %s"
        params.append(status)
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        rows = conn.execute(
            "SELECT job_id, resume_id, user_id, cycle_id, status, attempts, error, "
            "card_version, qbank_status, created_at, updated_at "
            f"FROM evaluation_job WHERE {where} ORDER BY job_id DESC LIMIT 200",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def requeue_failed(cycle_id: int) -> list[int]:
    """失败 job 重回 pending(手动重试入口)。

    闸门3:attempts 达 _MAX_ATTEMPTS 的失败 job 不再自动重排(人工介入)。
    #175:同 (resume, cycle) 已有其他活跃 job 的旧失败行不复活;同组多条
    失败行只翻 job_id 最新的一条——两条失败行在同一 UPDATE 里同时变活跃
    会撞 uq_eval_job_active_resume,整批恢复失败。
    """
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        rows = conn.execute(
            """
            UPDATE evaluation_job SET status = 'pending', error = NULL, updated_at = now()
            WHERE cycle_id = %s AND status = 'failed' AND attempts < %s
              AND NOT EXISTS (
                  SELECT 1 FROM evaluation_job j2
                  WHERE j2.resume_id = evaluation_job.resume_id
                    AND j2.cycle_id = evaluation_job.cycle_id
                    AND j2.status IN ('pending', 'running')
                    AND j2.job_id <> evaluation_job.job_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM evaluation_job j3
                  WHERE j3.resume_id = evaluation_job.resume_id
                    AND j3.cycle_id = evaluation_job.cycle_id
                    AND j3.status = 'failed'
                    AND j3.attempts < %s
                    AND j3.job_id > evaluation_job.job_id
              )
            RETURNING job_id
            """,
            (cycle_id, _MAX_ATTEMPTS, _MAX_ATTEMPTS),
        ).fetchall()
    return [int(r["job_id"]) for r in rows]


def requeue_stale(cycle_id: int, *, older_than_minutes: int = 10) -> list[int]:
    """残留恢复(进程重启后 pending/running 僵 job):超过时限才回 pending。

    时限防误伤:刚提交的 pending/running 有活任务在跑,重入队会双跑。
    闸门3:attempts 达到 _MAX_ATTEMPTS 的 job 不再自动重排(标 failed 交人工),
    避免死循环无限重试。#175:同 (resume, cycle) 已有其他活跃 job 的行不
    重排,防 legacy 重复 job 撞部分唯一索引。
    """
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        # 1) 超限的僵 job 直接落 failed(error 标注),不进自动重排
        conn.execute(
            """
            UPDATE evaluation_job SET status = 'failed',
                error = COALESCE(error, '') || ' [attempts 超上限,转人工]',
                updated_at = now()
            WHERE cycle_id = %s AND status IN ('pending', 'running')
              AND attempts >= %s
              AND updated_at < now() - (%s || ' minutes')::interval
            """,
            (cycle_id, _MAX_ATTEMPTS, str(older_than_minutes)),
        )
        # 2) 未超限的僵 job 回 pending。#175 双守卫:兄弟已活跃不复活;
        #    同组多条失败行只翻最新一条(防同语句双激活撞唯一索引)
        rows = conn.execute(
            """
            UPDATE evaluation_job SET status = 'pending', error = NULL, updated_at = now()
            WHERE cycle_id = %s AND status IN ('failed', 'pending', 'running')
              AND attempts < %s
              AND updated_at < now() - (%s || ' minutes')::interval
              AND NOT EXISTS (
                  SELECT 1 FROM evaluation_job j2
                  WHERE j2.resume_id = evaluation_job.resume_id
                    AND j2.cycle_id = evaluation_job.cycle_id
                    AND j2.status IN ('pending', 'running')
                    AND j2.job_id <> evaluation_job.job_id
              )
              AND NOT (
                  evaluation_job.status = 'failed'
                  AND EXISTS (
                      SELECT 1 FROM evaluation_job j3
                      WHERE j3.resume_id = evaluation_job.resume_id
                        AND j3.cycle_id = evaluation_job.cycle_id
                        AND j3.status = 'failed'
                        AND j3.attempts < %s
                        AND j3.updated_at < now() - (%s || ' minutes')::interval
                        AND j3.job_id > evaluation_job.job_id
                  )
              )
            RETURNING job_id
            """,
            (
                cycle_id,
                _MAX_ATTEMPTS,
                str(older_than_minutes),
                _MAX_ATTEMPTS,
                str(older_than_minutes),
            ),
        ).fetchall()
    return [int(r["job_id"]) for r in rows]


def requeue_stale_all_cycles(*, older_than_minutes: int = 10) -> list[dict[str, Any]]:
    """闸门3 启动自动恢复:不限周期,把超时限的僵 job 全部回 pending。

    返回 job_id + cycle_id 供派发;attempts 达上限的落 failed 交人工。
    #175:同 (resume, cycle) 已有其他活跃 job 的行不重排,防 legacy
    重复 job 在恢复时撞部分唯一索引、整批恢复失败。
    """
    with _conn() as conn:
        ensure_evaluation_job_table(conn)
        # 超限僵 job 落 failed(不进自动重排)
        conn.execute(
            """
            UPDATE evaluation_job SET status = 'failed',
                error = COALESCE(error, '') || ' [attempts 超上限,转人工]',
                updated_at = now()
            WHERE status IN ('pending', 'running')
              AND attempts >= %s
              AND updated_at < now() - (%s || ' minutes')::interval
            """,
            (_MAX_ATTEMPTS, str(older_than_minutes)),
        )
        # 未超限僵 job 回 pending。#175 双守卫:兄弟已活跃不复活;
        # 同组多条失败行只翻最新一条(防同语句双激活撞唯一索引)
        rows = conn.execute(
            """
            UPDATE evaluation_job SET status = 'pending', error = NULL, updated_at = now()
            WHERE status IN ('failed', 'pending', 'running')
              AND attempts < %s
              AND updated_at < now() - (%s || ' minutes')::interval
              AND NOT EXISTS (
                  SELECT 1 FROM evaluation_job j2
                  WHERE j2.resume_id = evaluation_job.resume_id
                    AND j2.cycle_id = evaluation_job.cycle_id
                    AND j2.status IN ('pending', 'running')
                    AND j2.job_id <> evaluation_job.job_id
              )
              AND NOT (
                  evaluation_job.status = 'failed'
                  AND EXISTS (
                      SELECT 1 FROM evaluation_job j3
                      WHERE j3.resume_id = evaluation_job.resume_id
                        AND j3.cycle_id = evaluation_job.cycle_id
                        AND j3.status = 'failed'
                        AND j3.attempts < %s
                        AND j3.updated_at < now() - (%s || ' minutes')::interval
                        AND j3.job_id > evaluation_job.job_id
                  )
              )
            RETURNING job_id, cycle_id
            """,
            (
                _MAX_ATTEMPTS,
                str(older_than_minutes),
                _MAX_ATTEMPTS,
                str(older_than_minutes),
            ),
        ).fetchall()
    return [dict(r) for r in rows]


def list_review_queue(cycle_id: int, queue: str = "all") -> list[dict[str, Any]]:
    """评审队列投影(#128):每简历最新卡 + 关联 user_id(勾选重评需要,#154)。

    queue=zero → 仅初筛不过(hard_zero)子队列;all → 全部。
    """
    with _conn() as conn:
        ensure_evaluation_tables(conn)
        outer = "WHERE hard_zero = TRUE" if queue == "zero" else ""
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT DISTINCT ON (resume_id)
                    s.resume_id, s.card_version, s.status, s.hard_zero, s.total,
                    s.prompt_version, s.created_at,
                    (SELECT j.user_id FROM evaluation_job j
                      WHERE j.resume_id = s.resume_id AND j.cycle_id = %s
                      ORDER BY j.job_id DESC LIMIT 1) AS user_id
                FROM evaluation_scorecard s WHERE s.cycle_id = %s
                ORDER BY resume_id, card_version DESC
            ) latest {outer}
            ORDER BY resume_id
            """,
            (cycle_id, cycle_id),
        ).fetchall()
        return [dict(r) for r in rows]
