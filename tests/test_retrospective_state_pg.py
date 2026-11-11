"""周期复盘状态面的真库集成测试:快照表的自举/版本/幂等,以及各域聚合 SQL。

需要真 PostgreSQL(POSTGRES_URL);无 PG 时整文件 skip。

全部用例跑在一个**一次性的 schema** 里(search_path 指过去,结束时整个 DROP):
- 表不存在时的自举要从「没有这张表」开始验,而 POSTGRES_URL 可能指着开发库,
  不能去 DROP 那里的 cycle_metrics_snapshot
- 对话日志、检索日志按时间窗聚合,混在公共表里会数到别的用例写的行
"""

import json
import secrets
import threading
from datetime import datetime, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.kb import query_log  # noqa: E402
from official_agent.retrospective import catalog, metrics  # noqa: E402
from official_agent.state import conversation, pools  # noqa: E402
from official_agent.state import evaluation as ev_store  # noqa: E402
from official_agent.state import qbank as qbank_store  # noqa: E402
from official_agent.state import retrospective as store  # noqa: E402
from official_agent.state.retrospective import snapshot_store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")

SCHEMA = f"retro_test_{secrets.token_hex(4)}"
SCOPED_URL = (
    f"{PG_URL}{'&' if '?' in PG_URL else '?'}options=-c%20search_path%3D{SCHEMA}" if PG_URL else ""
)

CYCLE = 12
OTHER_CYCLE = 13
WINDOW = metrics.cycle_window(datetime(2026, 9, 1).date(), datetime(2026, 9, 30).date())
INSIDE = WINDOW.start + timedelta(days=9, hours=12)


def _reset_connection_caches() -> None:
    pools.reset_pools()
    ev_store.reset_pool()
    query_log.reset_table_cache()
    snapshot_store.reset_table_cache()


@pytest.fixture(scope="module", autouse=True)
def _scratch_schema():
    """建一次性 schema 并把全局配置指过去;结束时连 schema 带表一起删。"""
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {SCHEMA}")
    previous = point_settings_at_pg(SCOPED_URL)
    _reset_connection_caches()
    try:
        conversation.ensure_conversation_table()
        query_log.ensure_query_log_table()
        ev_store.ensure_evaluation_scorecard_ready()
        ev_store.ensure_evaluation_job_ready()
        qbank_store.ensure_qbank_ready()
        yield
    finally:
        point_settings_at_pg(previous)
        _reset_connection_caches()
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@pytest.fixture(autouse=True)
def _empty_tables():
    """每个用例从空表开始(快照表可能还没建,单独处理)。"""
    with psycopg.connect(SCOPED_URL, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE agent_conversation_log, kb_query_log, evaluation_job, "
            "evaluation_scorecard, interview_qbank, qbank_pick_log"
        )
        conn.execute("DROP TABLE IF EXISTS cycle_metrics_snapshot")
    snapshot_store.reset_table_cache()


def _execute(sql: str, params: tuple = ()) -> None:
    with psycopg.connect(SCOPED_URL, autocommit=True) as conn:
        conn.execute(sql, params)


def _table_exists(name: str) -> bool:
    with psycopg.connect(SCOPED_URL) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (SCHEMA, name),
        ).fetchone()
    return row is not None


def _body(marker: int = 1) -> dict:
    return {
        "schema": catalog.METRICS_SCHEMA,
        "cycle_id": CYCLE,
        "domains": {"funnel": {"status": "ok"}, "kb": {"status": "unavailable", "reason": "x"}},
        "metrics": [
            {"id": "funnel.resumes_total", "value": 200 + marker, "unit": "count"},
            {"id": "screening.adopted_by_dept[技术部]", "value": 60, "unit": "count"},
        ],
    }


def _save(body: dict, *, cycle_id: int = CYCLE, schema: str = catalog.METRICS_SCHEMA, by: int = 7):
    return store.save_snapshot(cycle_id=cycle_id, metrics_schema=schema, metrics=body, taken_by=by)


# ── 快照表:自举 ────────────────────────────────────────


def test_snapshot_table_is_created_on_a_database_that_never_had_it() -> None:
    """存量库升级:表不存在时,第一次用到就建出来,不需要手工迁移。"""
    assert not _table_exists("cycle_metrics_snapshot")
    record, created = _save(_body())
    assert _table_exists("cycle_metrics_snapshot")
    assert (record.version, created) == (1, True)


def test_snapshot_bootstrap_is_idempotent_and_keeps_existing_rows() -> None:
    _save(_body())
    snapshot_store.reset_table_cache()  # 模拟进程重启:再跑一遍建表
    store.ensure_snapshot_table()
    store.ensure_snapshot_table()
    assert store.get_snapshot(CYCLE) is not None


def test_snapshot_read_on_fresh_database_returns_nothing_instead_of_failing() -> None:
    assert store.get_snapshot(CYCLE) is None
    assert store.list_snapshots(CYCLE) == []


# ── 快照表:版本与幂等 ──────────────────────────────────


def test_same_content_is_saved_once() -> None:
    first, created_first = _save(_body())
    again, created_again = _save(_body(), by=99)
    assert (created_first, created_again) == (True, False)
    assert again.version == first.version == 1
    assert again.taken_by == 7 and again.taken_at == first.taken_at  # 已有那一版原样返回
    assert len(store.list_snapshots(CYCLE)) == 1


def test_changed_content_appends_a_version_and_keeps_the_old_one() -> None:
    _save(_body(1))
    second, created = _save(_body(2))
    assert (second.version, created) == (2, True)

    latest = store.get_snapshot(CYCLE)
    assert latest is not None and latest.version == 2
    assert latest.metrics == _body(2)  # jsonb 往返:中文键、数字原样
    original = store.get_snapshot(CYCLE, version=1)
    assert original is not None and original.metrics == _body(1)

    listed = store.list_snapshots(CYCLE)
    assert [item["version"] for item in listed] == [2, 1]
    assert listed[0]["domains"]["kb"] == {"status": "unavailable", "reason": "x"}
    assert "metrics" not in listed[0]


def test_reverting_to_earlier_content_still_appends() -> None:
    """只和最新一版比:内容改回去是一次新的采集事实,不折叠到旧版本上。"""
    _save(_body(1))
    _save(_body(2))
    third, created = _save(_body(1))
    assert (third.version, created) == (3, True)


def test_versions_are_counted_per_cycle() -> None:
    _save(_body(1))
    _save(_body(2))
    other, _ = _save(_body(1), cycle_id=OTHER_CYCLE)
    assert other.version == 1
    assert store.get_snapshot(OTHER_CYCLE).cycle_id == OTHER_CYCLE


def test_get_by_metric_schema_skips_other_definitions() -> None:
    """跨周期对比要拿同一版指标定义的快照:更新的一版若换了定义,不被选中。"""
    _save(_body(1), schema="cycle_metrics/v1")
    _save(_body(2), schema="cycle_metrics/v2")
    picked = store.get_snapshot(CYCLE, metrics_schema="cycle_metrics/v1")
    assert picked is not None and picked.version == 1
    assert store.get_snapshot(CYCLE, metrics_schema="cycle_metrics/v9") is None


def test_concurrent_saves_both_land_with_distinct_versions() -> None:
    store.ensure_snapshot_table()
    barrier = threading.Barrier(2)
    results: list[tuple[int, bool]] = []
    errors: list[Exception] = []

    def _worker(marker: int) -> None:
        try:
            barrier.wait(timeout=5)
            record, created = _save(_body(marker))
            results.append((record.version, created))
        except Exception as exc:  # noqa: BLE001 — 收集后在主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(m,)) for m in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    assert sorted(results) == [(1, True), (2, True)]


# ── 对话域聚合 ──────────────────────────────────────────


def _turn(thread: str, *, at: datetime = INSIDE, channel: str = "web", **cols) -> None:
    columns = {
        "thread_id": thread,
        "channel": channel,
        "created_at": at,
        "tools": json.dumps(cols.pop("tools", [])),
        **cols,
    }
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    _execute(
        f"INSERT INTO agent_conversation_log ({names}) VALUES ({marks})",  # noqa: S608
        tuple(columns.values()),
    )


def test_conversation_aggregates_count_turns_in_the_window() -> None:
    _turn("t1", duration_ms=100, input_tokens=1000, output_tokens=100,
          cache_hit_tokens=600, cache_miss_tokens=400,
          tools=["search_knowledge", "search_knowledge", "get_open_cycle"])
    _turn("t1", duration_ms=200, input_tokens=2000, output_tokens=200,
          cache_hit_tokens=1500, cache_miss_tokens=500, tools=["search_knowledge"])
    _turn("t2", duration_ms=300, error_code="timeout")
    _turn("t3", channel="cli", duration_ms=400, input_tokens=500, output_tokens=None,
          cache_hit_tokens=100, cache_miss_tokens=None)
    _turn("t4")  # 没有耗时也没有用量的一轮
    # 以下都不该被数进去
    _turn("eval:77", channel="evaluation", input_tokens=9_999_999, output_tokens=1)
    _turn("early", at=WINDOW.start - timedelta(seconds=1), duration_ms=99_999)
    _turn("late", at=WINDOW.end, duration_ms=99_999)  # 窗口右端不含

    agg = store.conversation_aggregates(WINDOW.start, WINDOW.end)

    assert agg["turns"] == 5
    assert agg["sessions"] == 4
    assert agg["error_turns"] == 1
    assert agg["timed_turns"] == 4
    # percentile_cont 在 [100, 200, 300, 400] 上线性插值
    assert agg["duration_p50_ms"] == pytest.approx(250.0)
    assert agg["duration_p90_ms"] == pytest.approx(370.0)
    assert agg["duration_p99_ms"] == pytest.approx(397.0)
    assert agg["metered_turns"] == 3
    assert agg["input_tokens"] == 3500
    assert agg["output_tokens"] == 300
    # 缓存两项只算都有记录的轮:t3 那轮只有命中没有未命中,不计
    assert agg["cache_turns"] == 2
    assert (agg["cache_hit_tokens"], agg["cache_miss_tokens"]) == (2100, 900)
    assert agg["by_channel"] == {"web": 4, "cli": 1}
    assert agg["tool_calls"] == {"search_knowledge": 3, "get_open_cycle": 1}


def test_conversation_aggregates_window_start_is_inclusive() -> None:
    _turn("edge", at=WINDOW.start)
    assert store.conversation_aggregates(WINDOW.start, WINDOW.end)["turns"] == 1


def test_conversation_aggregates_on_empty_window() -> None:
    agg = store.conversation_aggregates(WINDOW.start, WINDOW.end)
    assert agg["turns"] == 0 and agg["duration_p50_ms"] is None
    assert agg["input_tokens"] == 0 and agg["by_channel"] == {} and agg["tool_calls"] == {}
    # 空窗口也能走完口径函数,不除零
    assert metrics.conversation_metrics(agg)


# ── 知识库域聚合 ────────────────────────────────────────


def _search(status: str, score: float | None, *, at: datetime = INSIDE) -> None:
    _execute(
        "INSERT INTO kb_query_log (thread_id, trace_id, query, status, best_vec_score, created_at)"
        " VALUES ('t', 'trace', '问题', %s, %s, %s)",
        (status, score, at),
    )


def test_kb_aggregates_split_outcomes_and_low_score_hits() -> None:
    _search("ok", 0.80)
    _search("ok", 0.45)  # 恰在门槛上:是靠向量分过关的,不算低分
    _search("ok", 0.30)  # 向量分没过门槛,只靠关键词命中
    _search("ok", 0.44)
    _search("not_found", 0.41)
    _search("not_found", 0.10)
    _search("unavailable", None)
    _search("ok", 0.20, at=WINDOW.end + timedelta(days=1))  # 窗口外

    agg = store.kb_aggregates(WINDOW.start, WINDOW.end, min_score=0.45)

    assert agg == {
        "searches": 7,
        "hits": 4,
        "not_found": 2,
        "unavailable": 1,
        "low_score_hits": 2,
    }


# ── 初筛域聚合 ──────────────────────────────────────────


def _card(first_dept: str | None, *, hard_zero: bool = False) -> dict:
    return {
        "schema": "evaluation_scorecard/v2",
        "intended": {"first": first_dept, "second": None},
        "hard_zero": hard_zero,
        "total": None,
    }


def _scorecard(resume_id: int, first_dept: str | None, *, cycle_id: int = CYCLE,
               status: str = "draft", hard_zero: bool = False) -> int:
    version = ev_store.save_scorecard(
        _card(first_dept, hard_zero=hard_zero),
        resume_id=resume_id, cycle_id=cycle_id, prompt_version="test",
    )
    if status != "draft":
        ev_store.set_scorecard_status(resume_id, cycle_id, version, status)
    return version


def _job(resume_id: int, *, cycle_id: int = CYCLE, runs: int = 1, final: str = "succeeded") -> int:
    (job_id,) = ev_store.create_jobs([(resume_id, 1000 + resume_id)], cycle_id)
    for _ in range(runs):
        ev_store.mark_job(job_id, "running")  # 每进一次 running,attempts 加一
    ev_store.mark_job(job_id, final)
    return job_id


def test_screening_aggregates() -> None:
    _job(1)
    _job(2, runs=2)
    _job(3, runs=3, final="failed")
    _job(4, runs=0, final="pending")
    _job(1, cycle_id=OTHER_CYCLE)  # 别的周期

    _scorecard(1, "技术部", status="adopted")
    _scorecard(2, "技术部", status="rejected")
    _scorecard(2, "技术部", hard_zero=True)  # 复评:最新一版是 draft 且 hard_zero
    _scorecard(3, "媒体部", status="rejected")
    _scorecard(3, "媒体部", status="adopted")  # 先驳回后采纳:以最后一次决定为准
    _scorecard(4, None, status="adopted")  # 卡上没有志愿
    _scorecard(5, "项目部")  # 还没人看
    _scorecard(9, "技术部", cycle_id=OTHER_CYCLE, status="adopted")

    agg = store.screening_aggregates(CYCLE)

    assert agg["jobs"] == 4
    assert agg["failed_jobs"] == 1
    assert agg["by_attempts"] == {0: 1, 1: 1, 2: 1, 3: 1}
    assert agg["scored_resumes"] == 5
    assert agg["hard_zero_resumes"] == 1  # 只看最新一版
    assert agg["card_versions"] == 7
    assert agg["decisions"] == {
        ("adopted", "技术部"): 1,
        ("rejected", "技术部"): 1,  # 复评后的新 draft 卡没有抹掉先前的驳回
        ("adopted", "媒体部"): 1,
        ("adopted", None): 1,
    }

    values = {
        v.id: v
        for item in metrics.screening_metrics(agg)
        for v in metrics.suppress(item)
    }
    # 这个周期只有 5 份简历:按部门拆的每一格都是小格子,一个也不出来
    assert all(v.suppressed for mid, v in values.items() if "_by_dept" in mid)


# ── 出题域聚合 ──────────────────────────────────────────


def _envelope(*, degraded: bool, extra_group: bool = False) -> dict:
    groups: list[dict] = [
        {
            "group": "repo",
            "qbank_v2": {
                "degraded": degraded,
                "group": {
                    "entry": {"category": "架构", "question": "入口题"},
                    "chains": [
                        {"category": "实现", "layers": [{"question": "追问一"},
                                                       {"question": "追问二"}]}
                    ],
                    "reserves": [{"category": "备用", "question": "备用题"}],
                },
            },
        }
    ]
    if extra_group:
        groups.append(
            {"group": "awards", "questions": [{"anchor": "奖项", "question": "奖项题"}]}
        )
    return {"schema_name": "evaluation_qbank/v2", "groups": groups}


def _qbank(resume_id: int, envelope: dict, *, cycle_id: int = CYCLE) -> None:
    qbank_store.save_qbank(
        resume_id=resume_id, cycle_id=cycle_id, source="repo",
        envelope=envelope, prompt_version="test",
    )


def _pick(resume_id: int, questions: list[str], *, cycle_id: int = CYCLE) -> None:
    resolved = qbank_store.resolve_picks(
        resume_id, cycle_id, [{"question": q} for q in questions]
    )
    qbank_store.record_picks(
        resume_id=resume_id, cycle_id=cycle_id, interviewer_user_id=501,
        question_refs=resolved,
    )


def test_qbank_aggregates() -> None:
    _qbank(1, _envelope(degraded=False, extra_group=True))  # 5 道:repo 4 + awards 1
    _pick(1, ["入口题", "追问一"])
    _pick(1, ["入口题"])  # 同一道题被另一位面试官再选一次:按题去重,不重复计

    _qbank(2, _envelope(degraded=True))  # 旧版(降级)
    _pick(2, ["备用题"])  # 选在旧版上
    _qbank(2, _envelope(degraded=False))  # 重新生成:最新一版不降级,旧版的选题对不上

    _qbank(3, _envelope(degraded=True))  # 有题库,没人选题
    _qbank(4, _envelope(degraded=True), cycle_id=OTHER_CYCLE)

    agg = store.qbank_aggregates(CYCLE)

    assert agg["generated"] == 3
    assert agg["degraded"] == 1  # 只有简历 3 的最新一版带降级标记
    assert agg["picked_resumes"] == 2
    # 可挑题只算有选题记录的简历 1、2 的最新一版:repo 4 + 4,awards 1
    assert agg["offered_by_group"] == {"repo": 8, "awards": 1}
    assert agg["picked_by_group"] == {"repo": 2}


def test_qbank_aggregates_on_empty_cycle() -> None:
    assert store.qbank_aggregates(CYCLE) == {
        "generated": 0,
        "degraded": 0,
        "picked_resumes": 0,
        "offered_by_group": {},
        "picked_by_group": {},
    }


# ── 评估用量聚合 ────────────────────────────────────────


def test_evaluation_usage_is_attributed_to_the_cycle_through_the_job() -> None:
    job_a = _job(1)
    job_b = _job(2)
    job_other = _job(3, cycle_id=OTHER_CYCLE)
    _turn(f"eval:{job_a}", channel="evaluation", input_tokens=1000, output_tokens=100)
    _turn(f"eval:{job_a}", channel="evaluation", input_tokens=500, output_tokens=50)  # 重跑
    _turn(f"eval:{job_b}", channel="evaluation")  # 没有仓线,用量全空
    _turn(f"eval:{job_other}", channel="evaluation", input_tokens=7000, output_tokens=700)
    _turn("eval:not-a-job", channel="evaluation", input_tokens=9, output_tokens=9)
    _turn(f"eval:{job_a}", channel="web", input_tokens=9, output_tokens=9)  # 不是用量行

    assert store.evaluation_usage_aggregates(CYCLE) == {
        "metered_resumes": 1,
        "input_tokens": 1500,
        "output_tokens": 150,
    }


# ── 端到端:真库上采集、落快照、原料被清理后快照仍在 ──────


@pytest.fixture
def backend(monkeypatch):
    async def _window(_cycle_id: int):
        return WINDOW

    async def _stats(cycle_id: int):
        return {
            "cycleId": cycle_id,
            "totalResumes": 40,
            "totalResults": 30,
            "decisionCounts": {"pending": 5, "passed": 15, "rejected": 10, "toTransfer": 0},
            "assignedByDeptId": {"1": 20, "2": 10},
            "evaluatedCandidates": 25,
        }

    monkeypatch.setattr(metrics, "fetch_cycle_window", _window)
    monkeypatch.setattr(metrics.readonly, "get_recruit_statistics", _stats)


@pytest.mark.asyncio
async def test_collect_and_save_end_to_end(backend) -> None:
    for i in range(8):
        _turn(f"web:u{i}", duration_ms=1000 + i, input_tokens=100, output_tokens=10,
              tools=["search_knowledge"])
        _search("ok" if i < 6 else "not_found", 0.7)

    payload = await metrics.collect_metrics(CYCLE)
    record, created = _save(payload)
    assert (record.version, created) == (1, True)

    stored = {m["id"]: m for m in store.get_snapshot(CYCLE).metrics["metrics"]}
    assert stored["funnel.resumes_total"]["value"] == 40
    assert stored["conversation.turns"]["value"] == 8
    assert stored["conversation.tool_calls[search_knowledge]"]["value"] == 8
    assert stored["kb.searches"]["value"] == 8
    assert stored["kb.not_found_rate"]["suppressed"] is True  # 2/8:分子不足 5

    # 数据没变,再采一次:不落新版本
    again, created_again = _save(await metrics.collect_metrics(CYCLE))
    assert (again.version, created_again) == (1, False)

    # 多了一轮对话,再采:落第 2 版
    _turn("web:u99", duration_ms=5, tools=[])
    newer, created_newer = _save(await metrics.collect_metrics(CYCLE))
    assert (newer.version, created_newer) == (2, True)


@pytest.mark.asyncio
async def test_snapshot_outlives_the_logs_it_was_computed_from(backend) -> None:
    """对话日志与检索日志随会话删除;删完之后,快照里的数还在。"""
    for i in range(6):
        _turn("web:u7:abc", duration_ms=100, tools=[])
        _execute(
            "INSERT INTO kb_query_log (thread_id, trace_id, query, status, created_at)"
            " VALUES ('web:u7:abc', 'trace', '问题', 'ok', %s)",
            (INSIDE + timedelta(seconds=i),),
        )
    record, _ = _save(await metrics.collect_metrics(CYCLE))

    assert conversation.delete_thread_conversations("web:u7:abc") == 6
    assert query_log.delete_thread_queries("web:u7:abc") == 6
    assert store.conversation_aggregates(WINDOW.start, WINDOW.end)["turns"] == 0

    kept = store.get_snapshot(CYCLE)
    assert kept is not None and kept.content_hash == record.content_hash
    values = {m["id"]: m["value"] for m in kept.metrics["metrics"]}
    assert values["conversation.turns"] == 6
    assert values["kb.searches"] == 6


@pytest.mark.asyncio
async def test_missing_source_table_degrades_that_domain_only(backend) -> None:
    """检索日志表还没建过(升级后没人查过知识库):知识库域标注缺席,其余照常。"""
    for i in range(6):
        _turn(f"web:u{i}", duration_ms=100, tools=[])
    _execute("ALTER TABLE kb_query_log RENAME TO kb_query_log_parked")
    try:
        payload = await metrics.collect_metrics(CYCLE)
    finally:
        _execute("ALTER TABLE kb_query_log_parked RENAME TO kb_query_log")

    assert payload["domains"]["kb"] == {
        "status": "unavailable",
        "reason": "agent 数据库查询失败(UndefinedTable)",
    }
    assert payload["domains"]["conversation"] == {"status": "ok"}
    assert {m["id"]: m for m in payload["metrics"]}["conversation.turns"]["value"] == 6


# ── 接口 + 真库往返 ─────────────────────────────────────


def test_admin_api_roundtrip_on_real_database(backend, monkeypatch, web_no_real_pg) -> None:
    """走真实路由与真实快照表:触发两次只落一版,读回来的就是存进去的。"""
    import contextlib

    from fastapi.testclient import TestClient

    from official_agent.web.app import create_app

    @contextlib.asynccontextmanager
    async def _no_checkpointer():
        yield None

    async def _resolve(*_a, **_k):
        return {
            "user_id": 42,
            "role": "admin",
            "role_names": ["管理员"],
            "permission_codes": ["evaluation:run", "resume:audit"],
            "source": "web",
        }

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _no_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    for i in range(6):
        _turn(f"web:u{i}", duration_ms=100, tools=["search_knowledge"])
    auth = {"Authorization": "Bearer tok"}
    base = "/api/agent/admin/retrospective"

    with TestClient(create_app()) as client:
        first = client.post(f"{base}/snapshot", json={"cycle_id": CYCLE}, headers=auth)
        again = client.post(f"{base}/snapshot", json={"cycle_id": CYCLE}, headers=auth)
        read = client.get(f"{base}/snapshot", params={"cycle_id": CYCLE}, headers=auth)
        listed = client.get(f"{base}/snapshots", params={"cycle_id": CYCLE}, headers=auth)

    assert first.status_code == 200, first.text
    assert (first.json()["version"], first.json()["created"]) == (1, True)
    assert first.json()["taken_by"] == 42
    assert (again.json()["version"], again.json()["created"]) == (1, False)

    body = read.json()
    assert body["metrics_schema"] == catalog.METRICS_SCHEMA
    assert body["snapshot"] == first.json()["snapshot"]
    values = {m["id"]: m["value"] for m in body["snapshot"]["metrics"]}
    assert values["conversation.turns"] == 6
    assert values["funnel.resumes_total"] == 40
    assert datetime.fromisoformat(body["taken_at"]).tzinfo is not None

    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["domains"]["conversation"] == {"status": "ok"}
