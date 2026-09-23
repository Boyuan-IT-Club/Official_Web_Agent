"""evaluation_job / evaluation_scorecard 真库集成测试:幂等、attempts 上限、
恢复、事务边界、评审队列投影。

需要真 PostgreSQL(POSTGRES_URL 指向可写库);无 PG 时整文件 skip。
CI 由 .github/workflows/ci.yml 的 postgres service 提供;本地起一个
`pgvector/pgvector:pg17`(与生产同镜像)并导出 POSTGRES_URL 即可。

建表由 module 级 autouse fixture 在任何用例之前完成——数据路径上的
ensure_* 已经拿掉(DDL 持表锁,见 state/evaluation.py 顶部),干净库上
再靠用例自己撞出表来就只会得到 UndefinedTable。
"""

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import (  # noqa: E402
    PG_URL,
    PG_URL_WITH_LOCK_TIMEOUT,
    pg_available,
    point_settings_at_pg,
)

from official_agent.state import evaluation as ev_store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_tables():
    """建表一次,先于所有用例(含用例里 DELETE 清场的那一步)。"""
    previous = point_settings_at_pg()
    ev_store.reset_pool()
    try:
        ev_store.ensure_evaluation_scorecard_ready()
        ev_store.ensure_evaluation_job_ready()
        yield
    finally:
        point_settings_at_pg(previous)


@pytest.fixture
def cycle_id() -> int:
    """每个测试用独立 cycle_id,避免与其他测试/残留数据互相干扰。"""
    return uuid.uuid4().int % 2_000_000_000 + 1


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_url", PG_URL)
    # 池按 postgres_url 懒建;patch 后重建,保证池连接指向本测试的库
    ev_store.reset_pool()


def _cleanup(cycle_id: int) -> None:
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM evaluation_job WHERE cycle_id = %s", (cycle_id,))
        conn.execute("DELETE FROM evaluation_scorecard WHERE cycle_id = %s", (cycle_id,))


def test_create_jobs_is_idempotent_for_active(cycle_id: int) -> None:
    """幂等:同 (resume, cycle) 重复提交只保留一个活跃 job,返回同一 job_id。"""
    _cleanup(cycle_id)
    try:
        first = ev_store.create_jobs([(9001, 501)], cycle_id)
        second = ev_store.create_jobs([(9001, 501)], cycle_id)
        assert first == second, "活跃 job 应复用同一 job_id"
        with psycopg.connect(PG_URL) as conn:
            rows = conn.execute(
                "SELECT count(*) FROM evaluation_job "
                "WHERE cycle_id = %s AND status IN ('pending','running')",
                (cycle_id,),
            ).fetchone()
        assert rows[0] == 1, "活跃 job 只能有一条"
    finally:
        _cleanup(cycle_id)


def test_terminal_job_allows_new_version(cycle_id: int) -> None:
    """终态(succeeded)不拦复评——重建新 job。"""
    _cleanup(cycle_id)
    try:
        first = ev_store.create_jobs([(9002, 502)], cycle_id)
        ev_store.mark_job(first[0], "succeeded", card_version=1)
        second = ev_store.create_jobs([(9002, 502)], cycle_id)
        assert second != first, "终态复评应新建 job(新版本卡)"
    finally:
        _cleanup(cycle_id)


def test_requeue_skips_attempts_exhausted(cycle_id: int) -> None:
    """attempts 达上限的失败 job 不再自动重排。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9003, 503)], cycle_id)[0]
        for _ in range(ev_store._MAX_ATTEMPTS):
            ev_store.mark_job(job_id, "running")
        ev_store.mark_job(job_id, "failed", error="boom")
        assert ev_store.requeue_failed(cycle_id) == [], "超上限不应重排"
    finally:
        _cleanup(cycle_id)


def test_requeue_stale_recovers_within_cap(cycle_id: int) -> None:
    """未超上限的僵 job 被 requeue_stale 捡回(用 0 分钟守卫即刻生效)。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9004, 504)], cycle_id)[0]
        ev_store.mark_job(job_id, "running")
        recovered = ev_store.requeue_stale(cycle_id, older_than_minutes=0)
        assert job_id in recovered
        assert ev_store.get_job(job_id)["status"] == "pending"
    finally:
        _cleanup(cycle_id)


def test_requeue_stale_all_cycles_returns_original_cycle(cycle_id: int) -> None:
    """全量恢复返回行必须携带原 cycle_id,多周期互不串线。

    runner 曾把整行当 job_id、cycle 硬编码 0 派发,本测试钉住状态层契约:
    每行 {job_id, cycle_id} 与建 job 时的周期一致。"""
    other_cycle = (cycle_id + 1) % 2_000_000_000 + 1
    _cleanup(cycle_id)
    _cleanup(other_cycle)
    try:
        job_a = ev_store.create_jobs([(9014, 514)], cycle_id)[0]
        job_b = ev_store.create_jobs([(9015, 515)], other_cycle)[0]
        ev_store.mark_job(job_a, "running")
        ev_store.mark_job(job_b, "running")
        rows = {
            int(r["job_id"]): int(r["cycle_id"])
            for r in ev_store.requeue_stale_all_cycles(older_than_minutes=0)
        }
        assert rows.get(job_a) == cycle_id
        assert rows.get(job_b) == other_cycle
        assert ev_store.get_job(job_a)["status"] == "pending"
        assert ev_store.get_job(job_b)["status"] == "pending"
    finally:
        _cleanup(cycle_id)
        _cleanup(other_cycle)


def test_repeated_stale_recovery_is_stable(cycle_id: int) -> None:
    """重复恢复不产生第二条活跃 job、不重复累加 attempts。

    同一 (resume, cycle) 连续两轮全量恢复:活跃 job 唯一;attempts 在
    进入 running 时已 +1,requeue 只改状态,两轮恢复后不增长。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9016, 516)], cycle_id)[0]
        ev_store.mark_job(job_id, "running")
        baseline_attempts = ev_store.get_job(job_id)["attempts"]
        first = ev_store.requeue_stale_all_cycles(older_than_minutes=0)
        second = ev_store.requeue_stale_all_cycles(older_than_minutes=0)
        assert job_id in [int(r["job_id"]) for r in first]
        assert [int(r["job_id"]) for r in second].count(job_id) == 1, (
            "重复恢复同一 job 应恰好命中一次"
        )
        row = ev_store.get_job(job_id)
        assert row["attempts"] == baseline_attempts
        with psycopg.connect(PG_URL) as conn:
            active = conn.execute(
                "SELECT count(*) FROM evaluation_job "
                "WHERE resume_id = 9016 AND cycle_id = %s "
                "AND status IN ('pending','running')",
                (cycle_id,),
            ).fetchone()
        assert active[0] == 1
    finally:
        _cleanup(cycle_id)


def test_requeue_skips_failed_when_sibling_active(cycle_id: int) -> None:
    """守卫分支一:同 (resume, cycle) 已有活跃 job,failed 行不得复活。

    legacy 重复 job 场景:复活旧失败行会撞 uq_eval_job_active_resume,
    整批恢复失败。守卫必须让失败行保持 failed、活跃行不受影响。"""
    _cleanup(cycle_id)
    try:
        active_id = ev_store.create_jobs([(9018, 518)], cycle_id)[0]
        with psycopg.connect(PG_URL) as conn:
            row = conn.execute(
                "INSERT INTO evaluation_job (resume_id, user_id, cycle_id, status, "
                "attempts) VALUES (9018, 518, %s, 'failed', 0) RETURNING job_id",
                (cycle_id,),
            ).fetchone()
        failed_id = int(row[0])
        rows = ev_store.requeue_stale_all_cycles(older_than_minutes=0)
        ids = [int(r["job_id"]) for r in rows]
        assert active_id in ids
        assert failed_id not in ids, "兄弟活跃时 failed 行不得复活"
        assert ev_store.get_job(failed_id)["status"] == "failed"
    finally:
        _cleanup(cycle_id)


def test_requeue_failed_flips_only_newest_in_group(cycle_id: int) -> None:
    """守卫分支二:同组多条 failed 只翻 job_id 最新的一条。

    两条失败行在同一 UPDATE 里同时变活跃会撞唯一索引、整批失败;
    正确语义是只复活最新一条(复评以最新为准),旧的留 failed。"""
    _cleanup(cycle_id)
    try:
        with psycopg.connect(PG_URL) as conn:
            ev_store.ensure_evaluation_job_table(conn)
            ids = []
            for _ in range(2):
                row = conn.execute(
                    "INSERT INTO evaluation_job (resume_id, user_id, cycle_id, "
                    "status, attempts) VALUES (9019, 519, %s, 'failed', 0) "
                    "RETURNING job_id",
                    (cycle_id,),
                ).fetchone()
                ids.append(int(row[0]))
        older, newer = sorted(ids)
        retried = ev_store.requeue_failed(cycle_id)
        assert retried == [newer], f"只翻组内最新失败行,实际 {retried}"
        assert ev_store.get_job(older)["status"] == "failed"
        assert ev_store.get_job(newer)["status"] == "pending"
    finally:
        _cleanup(cycle_id)


def test_integrity_dedupes_existing_active(cycle_id: int) -> None:
    """幂等加固:旧库已有重复活跃 job 时,integrity 去重后能建唯一索引。"""
    _cleanup(cycle_id)
    try:
        # 绕过 create_jobs 的幂等,直接插两条活跃 job 模拟旧库脏数据
        with psycopg.connect(PG_URL) as conn:
            ev_store.ensure_evaluation_job_table(conn)
            conn.execute("DROP INDEX IF EXISTS uq_eval_job_active_resume")
            for _ in range(2):
                conn.execute(
                    "INSERT INTO evaluation_job (resume_id, user_id, cycle_id) VALUES (%s, %s, %s)",
                    (9005, 505, cycle_id),
                )
        with psycopg.connect(PG_URL) as conn:
            ev_store.ensure_evaluation_job_integrity(conn)
            active = conn.execute(
                "SELECT count(*) FROM evaluation_job "
                "WHERE cycle_id = %s AND status IN ('pending','running')",
                (cycle_id,),
            ).fetchone()
        assert active[0] == 1, "去重后只保留一条活跃"
    finally:
        _cleanup(cycle_id)


def test_qbank_status_persisted(cycle_id: int) -> None:
    """qbank_status 落库,管理面可见"有评分无题库"。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9006, 506)], cycle_id)[0]
        ev_store.mark_job(job_id, "running")
        ev_store.mark_job(job_id, "succeeded", card_version=1, qbank_status="failed")
        row = ev_store.get_job(job_id)
        assert row["status"] == "succeeded"
        assert row["qbank_status"] == "failed"
        assert ev_store.list_jobs(cycle_id)[0]["qbank_status"] == "failed"
    finally:
        _cleanup(cycle_id)


def test_qbank_error_persisted_and_cleared(cycle_id: int) -> None:
    """qbank_error 随失败落库、随成功清列;latest_job 不限状态可回捞。

    管理面题库 404 文案靠这列给真实原因——生产曾把权限 403 展示成
    "暂无题库",看似没数据、实则生成失败。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9007, 507)], cycle_id)[0]
        ev_store.mark_job(job_id, "running")
        ev_store.mark_job(
            job_id,
            "succeeded",
            card_version=1,
            qbank_status="failed",
            qbank_error="BackendError: 权限不足(code 2101)",
        )
        row = ev_store.get_job(job_id)
        assert row["qbank_error"] == "BackendError: 权限不足(code 2101)"
        assert ev_store.latest_job(9007, cycle_id)["job_id"] == job_id

        # 复评翻案:成功路径必须显式清列,否则 404 文案拿陈旧原因误导
        job2 = ev_store.create_jobs([(9007, 507)], cycle_id)[0]
        assert job2 != job_id
        ev_store.mark_job(job2, "running")
        ev_store.mark_job(
            job2, "succeeded", card_version=2, qbank_status="succeeded", qbank_error=None
        )
        assert ev_store.get_job(job2)["qbank_error"] is None
        assert ev_store.latest_job(9007, cycle_id)["job_id"] == job2
    finally:
        _cleanup(cycle_id)


# ── 数据路径不得再取表级 DDL 锁 ───────────────────────────


def _hold_row_exclusive(table: str):
    """开一条连接并持 ROW EXCLUSIVE 锁(= INSERT 取的锁),返回该连接。

    ROW EXCLUSIVE 与自身相容、与 AccessShare(SELECT)相容,但与
    ShareLock(CREATE INDEX IF NOT EXISTS)和 AccessExclusiveLock
    (ALTER TABLE ... ADD COLUMN IF NOT EXISTS)冲突。于是:数据路径若
    还在跑建表 DDL,就会卡在这把锁上;不跑 DDL 则畅通无阻。
    """
    conn = psycopg.connect(PG_URL)
    conn.execute(f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE")  # noqa: S608
    return conn


@pytest.mark.parametrize("table", ["evaluation_job", "evaluation_scorecard"])
def test_data_path_takes_no_ddl_lock(table: str, cycle_id: int, monkeypatch) -> None:
    """并发写锁在手时,读写数据路径照常跑完(lock_timeout 2s 兜底)。

    回归钉:ensure_* 曾挂在每个数据函数的事务开头,一次纯读也会取
    ShareLock/AccessExclusiveLock,把整张表的读写串行化。
    """
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL_WITH_LOCK_TIMEOUT)
    ev_store.reset_pool()
    holder = _hold_row_exclusive(table)
    try:
        # 未提交的 LOCK 一直持有;以下调用必须在 2s 内返回而不是 lock timeout
        assert ev_store.list_jobs(cycle_id) == []
        assert ev_store.list_review_queue(cycle_id) == []
        assert ev_store.latest_scorecard(9101, cycle_id) is None
        assert ev_store.get_job(-1) is None
    finally:
        holder.rollback()
        holder.close()
        _cleanup(cycle_id)


def test_create_jobs_commits_per_item(cycle_id: int, monkeypatch) -> None:
    """批量创建逐条提交:第 3 条炸掉,前 2 条已落库。

    回归钉:整批(上限 200)一个事务会把这些行的锁持到最后一条提交,
    期间 worker 的 mark_job 和管理面的 job 列表全被挡住。
    """
    _cleanup(cycle_id)
    from official_agent.state.evaluation import job_store

    real_find = job_store._find_active_job
    seen: list[int] = []

    def _boom_on_third(conn, resume_id, cycle, *a, **kw):
        seen.append(resume_id)
        if len(seen) > 2:
            raise psycopg.OperationalError("模拟第 3 条失败")
        return real_find(conn, resume_id, cycle, *a, **kw)

    monkeypatch.setattr(job_store, "_find_active_job", _boom_on_third)
    try:
        with pytest.raises(psycopg.OperationalError):
            ev_store.create_jobs([(9101, 601), (9102, 602), (9103, 603)], cycle_id)
        monkeypatch.undo()
        survived = {int(j["resume_id"]) for j in ev_store.list_jobs(cycle_id)}
        assert survived == {9101, 9102}, f"前两条应已提交,实际 {survived}"
    finally:
        _cleanup(cycle_id)


# ── mark_job:只更新显式传入的列 ──────────────────────────


def test_mark_job_keeps_unpassed_columns(cycle_id: int) -> None:
    """失败路径不带 card_version 时,已落库的卡版本指针必须保留。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9104, 604)], cycle_id)[0]
        ev_store.mark_job(job_id, "running")
        ev_store.mark_job(job_id, "succeeded", card_version=7, qbank_status="succeeded")
        ev_store.mark_job(job_id, "failed", error="qbank 复跑炸了")
        row = ev_store.get_job(job_id)
        assert row["status"] == "failed"
        assert row["error"] == "qbank 复跑炸了"
        assert row["card_version"] == 7, "省略的列不该被置 NULL"
        assert row["qbank_status"] == "succeeded"
    finally:
        _cleanup(cycle_id)


def test_mark_job_clears_column_on_explicit_none(cycle_id: int) -> None:
    """显式传 None 仍然清列(重排时清 error 依赖这条)。"""
    _cleanup(cycle_id)
    try:
        job_id = ev_store.create_jobs([(9105, 605)], cycle_id)[0]
        ev_store.mark_job(job_id, "failed", error="boom")
        ev_store.mark_job(job_id, "pending", error=None)
        assert ev_store.get_job(job_id)["error"] is None
    finally:
        _cleanup(cycle_id)


def test_list_jobs_pages_with_offset(cycle_id: int) -> None:
    """分页:limit + offset 能翻到更早的 job(原先硬编码 LIMIT 200 无 offset)。"""
    _cleanup(cycle_id)
    try:
        ids = ev_store.create_jobs([(9106, 606), (9107, 607), (9108, 608)], cycle_id)
        page1 = ev_store.list_jobs(cycle_id, limit=2)
        page2 = ev_store.list_jobs(cycle_id, limit=2, offset=2)
        assert [j["job_id"] for j in page1] == sorted(ids, reverse=True)[:2]
        assert [j["job_id"] for j in page2] == sorted(ids, reverse=True)[2:]
    finally:
        _cleanup(cycle_id)


# ── 评审队列:复评不抹掉人工决策的痕迹 ─────────────────────


def _save_card(resume_id: int, cycle_id: int, *, total: float, hard_zero: bool = False) -> int:
    return ev_store.save_scorecard(
        {"total": total, "hard_zero": hard_zero},
        resume_id=resume_id,
        cycle_id=cycle_id,
        prompt_version="evaluation_scoring/v1",
    )


def test_review_queue_surfaces_prior_decision(cycle_id: int) -> None:
    """复评写新 draft 卡后,队列仍能看出这份简历此前已被采纳。

    回归钉:整周期重跑一次初筛,评审面上所有 adopted/rejected 都回退成
    draft,评审人分不清"没看过"和"看过已采纳"。
    """
    _cleanup(cycle_id)
    try:
        v1 = _save_card(9109, cycle_id, total=71.0)
        assert ev_store.set_scorecard_status(9109, cycle_id, v1, "adopted")
        v2 = _save_card(9109, cycle_id, total=64.0)  # 复评:新 draft 卡
        (item,) = ev_store.list_review_queue(cycle_id)
        assert item["card_version"] == v2
        assert item["status"] == "draft", "最新版本仍是 draft(复评合法,不禁止)"
        assert item["decided_status"] == "adopted"
        assert item["decided_version"] == v1
    finally:
        _cleanup(cycle_id)


def test_review_queue_marks_undecided_as_none(cycle_id: int) -> None:
    """没被人工处理过的简历,决策标记为空——与"采纳后又复评"可区分。"""
    _cleanup(cycle_id)
    try:
        _save_card(9110, cycle_id, total=55.0)
        (item,) = ev_store.list_review_queue(cycle_id)
        assert item["decided_status"] is None
        assert item["decided_version"] is None
    finally:
        _cleanup(cycle_id)


def test_review_queue_pages_and_filters_zero(cycle_id: int) -> None:
    """队列分页 + hard_zero 子队列过滤(原先无 LIMIT,整周期一次性拖回)。"""
    _cleanup(cycle_id)
    try:
        _save_card(9111, cycle_id, total=0.0, hard_zero=True)
        _save_card(9112, cycle_id, total=80.0)
        _save_card(9113, cycle_id, total=0.0, hard_zero=True)
        assert [i["resume_id"] for i in ev_store.list_review_queue(cycle_id, limit=2)] == [
            9111,
            9112,
        ]
        assert [
            i["resume_id"] for i in ev_store.list_review_queue(cycle_id, limit=2, offset=2)
        ] == [9113]
        assert [i["resume_id"] for i in ev_store.list_review_queue(cycle_id, "zero")] == [
            9111,
            9113,
        ]
    finally:
        _cleanup(cycle_id)


def test_review_queue_carries_user_id(cycle_id: int) -> None:
    """投影带 job 侧归属人(勾选重评要用),LATERAL 改写后不得丢。"""
    _cleanup(cycle_id)
    try:
        ev_store.create_jobs([(9114, 614)], cycle_id)
        _save_card(9114, cycle_id, total=66.0)
        (item,) = ev_store.list_review_queue(cycle_id)
        assert item["user_id"] == 614
    finally:
        _cleanup(cycle_id)


# ── 自举幂等 ───────────────────────────────────────────


def test_bootstrap_is_idempotent_and_drops_legacy_index() -> None:
    """重复自举不报错;冗余的 idx_eval_scorecard_resume 被清掉。

    它是 UNIQUE (resume_id, cycle_id, card_version) 的严格前缀,白占写放大。
    """
    with psycopg.connect(PG_URL) as conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_scorecard_resume "
            "ON evaluation_scorecard (resume_id, cycle_id)"
        )
    ev_store.ensure_evaluation_scorecard_ready()
    ev_store.ensure_evaluation_scorecard_ready()
    ev_store.ensure_evaluation_job_ready()
    with psycopg.connect(PG_URL) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'evaluation_scorecard'"
            ).fetchall()
        }
    assert "idx_eval_scorecard_resume" not in names
    assert "idx_eval_scorecard_cycle" in names
