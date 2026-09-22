"""evaluation_job 数据面:执行组织(状态机 + 幂等创建 + 僵 job 恢复)。

- 幂等创建:同 (resume, cycle) 只留一个活跃 job(部分唯一索引兜底)
- job 终态 succeeded 但 qbank_status=failed 时,管理面可见"有评分无题库"
- requeue 三入口共用同一组守卫 SQL(兄弟活跃不复活;同组只翻最新一条)
"""

from __future__ import annotations

from typing import Any

import psycopg

from official_agent.state.evaluation import _connection, bootstrap

# job 自动重试上限:对齐 tools/client 的 _MAX_ATTEMPTS;
# 超过即标 failed 并停止自动重排,交人工队列。
_MAX_ATTEMPTS = 3
# 评审队列/job 列表单页上限:防单次请求拖回整周期
_MAX_PAGE = 500
# 僵 job 判定的默认时限(分钟):刚提交的任务可能仍在跑,超时限才回 pending
_STALE_DEFAULT_MINUTES = 10

_BOOTSTRAP_KEY = "job"


class JobStoreError(RuntimeError):
    """job 表侧写入失败(DB 故障),区别于调用方数据错位。

    路由据此分流状态码:调用方数据错位是 400,本异常是 500——两者都曾是
    裸 RuntimeError,管理员看到的"简历归属核对失败"会盖住真正的库故障。
    RuntimeError 子类以兼容既有 `except RuntimeError` 调用点。
    """


def _ensure_bootstrapped() -> None:
    bootstrap.ensure_once(_BOOTSTRAP_KEY, _bootstrap)


def _bootstrap() -> None:
    with _connection._conn() as conn:
        ensure_evaluation_job_table(conn)
        ensure_evaluation_job_integrity(conn)


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
            qbank_error text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
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
    # 评审队列按 (resume_id, cycle_id) 回捞归属人:全状态覆盖,
    # 部分唯一索引 uq_eval_job_active_resume 只覆盖活跃行,接不住。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_job_resume "
        "ON evaluation_job (resume_id, cycle_id, job_id DESC)"
    )


def ensure_evaluation_job_integrity(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """启动加固(调用一次):补列 + 去重活跃 job + 建部分唯一索引。

    与 ensure_evaluation_job_table 分开的原因:
    - 去重是 O(表) 的 UPDATE,不该每次 DB 操作都跑;
    - CREATE UNIQUE INDEX 在旧库存在重复活跃 job 时会直接失败,必须先去重;
    - ALTER TABLE 即使 no-op 也取 AccessExclusiveLock(连 SELECT 都挡),
      只有启动窗口担得起;
    - create_jobs 自身用 SELECT-first + savepoint 保证正确性,
      本索引只是防御性兜底(并发竞态的最后一道闸)。
    """
    # 旧库自举:qbank_status 列对已存在表补加
    conn.execute("ALTER TABLE evaluation_job ADD COLUMN IF NOT EXISTS qbank_status text")
    # 旧库自举:qbank_error(题库线失败原因摘要,供管理面 404 文案)同窗口补加
    conn.execute("ALTER TABLE evaluation_job ADD COLUMN IF NOT EXISTS qbank_error text")
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
    """启动自举:建表 + 去重历史遗留的重复活跃 job + 建唯一索引。

    requeue 守卫只防"恢复时撞索引";重复活跃行的消除靠这里。lifespan 在
    启动恢复**之前**调用,保证恢复面对的是去重后的活跃集。
    重复调用无害(DDL 全 IF NOT EXISTS),并置位数据路径守卫。
    """
    with _connection._conn() as conn:
        ensure_evaluation_job_table(conn)
        ensure_evaluation_job_integrity(conn)
    bootstrap.mark_done(_BOOTSTRAP_KEY)


def create_jobs(items: list[tuple[int, int]], cycle_id: int) -> list[int]:
    """每份简历一行 job;幂等:同 (resume, cycle) 已有活跃 job → 返回已有 job_id。

    终态(succeeded/failed)不拦——复评是合法操作(新版本卡);活跃才去重。
    SELECT-first:先查活跃 job,有则直接复用;无则 INSERT。INSERT 包在
    savepoint 里,并发撞唯一键(uq_eval_job_active_resume)时回滚 savepoint
    并读回已有 job,不让事务进入 aborted 状态。

    **每条 item 一个事务**:批量上限 200,整批一个事务会把这 200 行的锁
    持到最后一条提交,期间 worker 的 mark_job / 管理面的 job 列表全被挡。
    逐条提交的代价是中途失败会留下部分 job——create_jobs 幂等,重试复用
    已建的活跃 job,不会重复建。
    """
    _ensure_bootstrapped()
    ids: list[int] = []
    with _connection._conn() as conn:
        for resume_id, user_id in items:
            with conn.transaction():  # 外层=真事务(逐条提交)
                ids.append(_create_one_job(conn, resume_id, user_id, cycle_id))
    return ids


def _create_one_job(
    conn: psycopg.Connection[dict[str, Any]], resume_id: int, user_id: int, cycle_id: int
) -> int:
    existing = _find_active_job(conn, resume_id, cycle_id)
    if existing is not None:
        return existing
    try:
        with conn.transaction():  # 内层=savepoint,撞唯一键只回滚这一步
            row = conn.execute(
                """
                INSERT INTO evaluation_job (resume_id, user_id, cycle_id)
                VALUES (%s, %s, %s) RETURNING job_id
                """,
                (resume_id, user_id, cycle_id),
            ).fetchone()
        if row:
            return int(row["job_id"])
    except psycopg.errors.UniqueViolation:
        pass  # 并发撞唯一键 → 读回已有活跃 job
    existing = _find_active_job(conn, resume_id, cycle_id)
    if existing is None:
        raise JobStoreError(
            f"创建 job 失败且无活跃 job 可复用(resume={resume_id}, cycle={cycle_id})"
        )
    return existing


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


class _Unset:
    """哨兵类型:区分"调用方没传这一列"与"显式传 None(清列)"。"""


_UNSET = _Unset()


def mark_job(
    job_id: int,
    status: str,
    *,
    error: str | None | _Unset = _UNSET,
    card_version: int | None | _Unset = _UNSET,
    qbank_status: str | None | _Unset = _UNSET,
    qbank_error: str | None | _Unset = _UNSET,
) -> bool:
    """状态迁移(pending→running→succeeded/failed;failed 可重试回 pending)。

    qbank_status:题库线独立完成态——succeeded/failed/skipped。
    job 终态 succeeded 但 qbank_status=failed 时,管理面可见"有评分无题库"。
    qbank_error:题库线失败原因摘要(错误类+文案),管理面 404 文案据此
    给出真实原因,不再拿"暂无题库"掩盖失败;成功路径显式清列防陈旧。

    只更新**显式传入**的列:失败路径只带 error,不该顺手把已落库的
    card_version 指针置空(卡还在,指针没了,管理面就查不到那一版)。
    要清列显式传 None。
    """
    if status not in ("pending", "running", "succeeded", "failed"):
        raise ValueError(f"非法 job 状态:{status!r}")
    sets = ["status = %s"]
    params: list[Any] = [status]
    for column, value in (
        ("error", error),
        ("card_version", card_version),
        ("qbank_status", qbank_status),
        ("qbank_error", qbank_error),
    ):
        if not isinstance(value, _Unset):
            sets.append(f"{column} = %s")
            params.append(value)
    sets.append("updated_at = now()")
    # attempts 语义=实际执行次数:只在进入 running 时累加
    if status == "running":
        sets.append("attempts = attempts + 1")
    params.append(job_id)
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        cur = conn.execute(
            f"UPDATE evaluation_job SET {', '.join(sets)} WHERE job_id = %s",  # noqa: S608
            params,
        )
        return cur.rowcount > 0


_JOB_COLUMNS = (
    "job_id, resume_id, user_id, cycle_id, status, attempts, error, "
    "card_version, qbank_status, qbank_error, created_at, updated_at"
)

# 共享守卫(SQL 片段):同 (resume, cycle) 的兄弟行已活跃则不复活——
# 双激活会撞部分唯一索引 uq_eval_job_active_resume,整批恢复失败。
_NOT_SIBLING_ACTIVE = """
  AND NOT EXISTS (
      SELECT 1 FROM evaluation_job j2
      WHERE j2.resume_id = evaluation_job.resume_id
        AND j2.cycle_id = evaluation_job.cycle_id
        AND j2.status IN ('pending', 'running')
        AND j2.job_id <> evaluation_job.job_id
  )
"""


def get_job(job_id: int) -> dict[str, Any] | None:
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        row = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM evaluation_job WHERE job_id = %s",  # noqa: S608
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def latest_job(resume_id: int, cycle_id: int) -> dict[str, Any] | None:
    """该 (resume, cycle) 最新一条 job(不限状态);无则 None。

    管理面题库 404 文案据此区分"从未跑过 / 跑了但题库线失败"——
    qbank 行缺席本身分不出这两种,原因在 job 行上。"""
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        row = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM evaluation_job "  # noqa: S608
            "WHERE resume_id = %s AND cycle_id = %s "
            "ORDER BY job_id DESC LIMIT 1",
            (resume_id, cycle_id),
        ).fetchone()
    return dict(row) if row else None


def list_jobs(
    cycle_id: int,
    *,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """按周期查 job(0 分队列由评审面按 scorecard.hard_zero 过滤,这里看执行面)。

    分页:job_id 倒序 + offset。无 offset 时超过一页的更早 job 无法回看。
    """
    where = "cycle_id = %s"
    params: list[Any] = [cycle_id]
    if status:
        where += " AND status = %s"
        params.append(status)
    params.extend([max(1, min(limit, _MAX_PAGE)), max(0, offset)])
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        rows = conn.execute(
            f"SELECT {_JOB_COLUMNS} "  # noqa: S608
            f"FROM evaluation_job WHERE {where} "  # noqa: S608
            "ORDER BY job_id DESC LIMIT %s OFFSET %s",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _fail_expired_stale(
    conn: psycopg.Connection[dict[str, Any]], cycle_id: int | None, older_than_minutes: int
) -> None:
    """超限(attempts ≥ 上限)的僵 job 直接落 failed(error 标注),不进自动重排。"""
    cycle_filter = "cycle_id = %s AND " if cycle_id is not None else ""
    params: list[Any] = [cycle_id] if cycle_id is not None else []
    params.extend([_MAX_ATTEMPTS, str(older_than_minutes)])
    conn.execute(
        f"""
        UPDATE evaluation_job SET status = 'failed',
            error = COALESCE(error, '') || ' [attempts 超上限,转人工]',
            updated_at = now()
        WHERE {cycle_filter}status IN ('pending', 'running')
          AND attempts >= %s
          AND updated_at < now() - (%s || ' minutes')::interval
        """,  # noqa: S608
        params,
    )


def _requeue_stale_rows(
    conn: psycopg.Connection[dict[str, Any]],
    cycle_id: int | None,
    older_than_minutes: int,
    *,
    returning_cycle: bool,
) -> list[dict[str, Any]]:
    """未超限的僵 job 回 pending(残留恢复主语句;requeue 两个入口共用)。

    双守卫:兄弟已活跃不复活;同组多条失败行只翻最新一条(防同语句
    双激活撞唯一索引)。cycle_id=None 恢复全周期(启动自动恢复)。
    """
    cycle_filter = "cycle_id = %s AND " if cycle_id is not None else ""
    params: list[Any] = [cycle_id] if cycle_id is not None else []
    params.extend([_MAX_ATTEMPTS, str(older_than_minutes), _MAX_ATTEMPTS, str(older_than_minutes)])
    rows = conn.execute(
        f"""
        UPDATE evaluation_job SET status = 'pending', error = NULL, updated_at = now()
        WHERE {cycle_filter}status IN ('failed', 'pending', 'running')
          AND attempts < %s
          AND updated_at < now() - (%s || ' minutes')::interval
        {_NOT_SIBLING_ACTIVE}
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
        RETURNING job_id{', cycle_id' if returning_cycle else ''}
        """,  # noqa: S608
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def requeue_failed(cycle_id: int) -> list[int]:
    """失败 job 重回 pending(手动重试入口)。

    attempts 达 _MAX_ATTEMPTS 的失败 job 不再自动重排(人工介入)。
    同 (resume, cycle) 已有其他活跃 job 的旧失败行不复活;同组多条
    失败行只翻 job_id 最新的一条——两条失败行在同一 UPDATE 里同时变活跃
    会撞 uq_eval_job_active_resume,整批恢复失败。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        rows = conn.execute(
            f"""
            UPDATE evaluation_job SET status = 'pending', error = NULL, updated_at = now()
            WHERE cycle_id = %s AND status = 'failed' AND attempts < %s
            {_NOT_SIBLING_ACTIVE}
              AND NOT EXISTS (
                  SELECT 1 FROM evaluation_job j3
                  WHERE j3.resume_id = evaluation_job.resume_id
                    AND j3.cycle_id = evaluation_job.cycle_id
                    AND j3.status = 'failed'
                    AND j3.attempts < %s
                    AND j3.job_id > evaluation_job.job_id
              )
            RETURNING job_id
            """,  # noqa: S608
            (cycle_id, _MAX_ATTEMPTS, _MAX_ATTEMPTS),
        ).fetchall()
    return [int(r["job_id"]) for r in rows]


def requeue_stale(
    cycle_id: int, *, older_than_minutes: int = _STALE_DEFAULT_MINUTES
) -> list[int]:
    """残留恢复(进程重启后 pending/running 僵 job):超过时限才回 pending。

    时限防误伤:刚提交的 pending/running 有活任务在跑,重入队会双跑。
    attempts 达到 _MAX_ATTEMPTS 的 job 不再自动重排(标 failed 交人工),
    避免死循环无限重试。同 (resume, cycle) 已有其他活跃 job 的行不
    重排,防历史遗留的重复 job 撞部分唯一索引。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        _fail_expired_stale(conn, cycle_id, older_than_minutes)
        rows = _requeue_stale_rows(conn, cycle_id, older_than_minutes, returning_cycle=False)
    return [int(r["job_id"]) for r in rows]


def requeue_stale_all_cycles(
    *, older_than_minutes: int = _STALE_DEFAULT_MINUTES
) -> list[dict[str, Any]]:
    """启动自动恢复:不限周期,把超时限的僵 job 全部回 pending。

    返回 job_id + cycle_id 供派发;attempts 达上限的落 failed 交人工。
    同 (resume, cycle) 已有其他活跃 job 的行不重排,防历史遗留的
    重复 job 在恢复时撞部分唯一索引、整批恢复失败。
    """
    _ensure_bootstrapped()
    with _connection._conn() as conn:
        _fail_expired_stale(conn, None, older_than_minutes)
        return _requeue_stale_rows(conn, None, older_than_minutes, returning_cycle=True)
