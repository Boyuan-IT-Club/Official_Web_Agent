"""evaluation_job 真库集成测试(闸门2/3):幂等、attempts 上限、恢复。

需要真 PostgreSQL(POSTGRES_URL 指向可写库);无 PG 时整文件 skip——
CI 由 .github/workflows/ci.yml 的 postgres service 提供,本地开发者
用 docker compose 起 PG 后自动生效(不设 PG 则跳过,不阻塞本地跑)。
"""

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from official_agent.state import evaluation as ev_store  # noqa: E402

PG_URL = os.environ.get("POSTGRES_URL", "")


def _pg_available() -> bool:
    if not PG_URL:
        return False
    try:
        with psycopg.connect(PG_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — 连不上即 skip
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")


@pytest.fixture
def cycle_id() -> int:
    """每个测试用独立 cycle_id,避免与其他测试/残留数据互相干扰。"""
    return uuid.uuid4().int % 2_000_000_000 + 1


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_url", PG_URL)


def _cleanup(cycle_id: int) -> None:
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM evaluation_job WHERE cycle_id = %s", (cycle_id,))


def test_create_jobs_is_idempotent_for_active(cycle_id: int) -> None:
    """闸门2:同 (resume, cycle) 重复提交只保留一个活跃 job,返回同一 job_id。"""
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
    """闸门2:终态(succeeded)不拦复评——重建新 job。"""
    _cleanup(cycle_id)
    try:
        first = ev_store.create_jobs([(9002, 502)], cycle_id)
        ev_store.mark_job(first[0], "succeeded", card_version=1)
        second = ev_store.create_jobs([(9002, 502)], cycle_id)
        assert second != first, "终态复评应新建 job(新版本卡)"
    finally:
        _cleanup(cycle_id)


def test_requeue_skips_attempts_exhausted(cycle_id: int) -> None:
    """闸门3:attempts 达上限的失败 job 不再自动重排。"""
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
    """闸门3:未超上限的僵 job 被 requeue_stale 捡回(用 0 分钟守卫即刻生效)。"""
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
    """#175:全量恢复返回行必须携带原 cycle_id,多周期互不串线。

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
    """#175:重复恢复不产生第二条活跃 job、不重复累加 attempts。

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
    """#175 守卫分支一:同 (resume, cycle) 已有活跃 job,failed 行不得复活。

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
    """#175 守卫分支二:同组多条 failed 只翻 job_id 最新的一条。

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
    """闸门2 加固:旧库已有重复活跃 job 时,integrity 去重后能建唯一索引。"""
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
    """闸门6:qbank_status 落库,管理面可见"有评分无题库"。"""
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
