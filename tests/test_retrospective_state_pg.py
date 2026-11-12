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
from official_agent.state import conversation, kb_ops, pools  # noqa: E402
from official_agent.state import evaluation as ev_store  # noqa: E402
from official_agent.state import qbank as qbank_store  # noqa: E402
from official_agent.state import retrospective as store  # noqa: E402
from official_agent.state.retrospective import report_store, snapshot_store  # noqa: E402

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
    kb_ops.reset_table_cache()
    snapshot_store.reset_table_cache()
    report_store.reset_table_cache()


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
        conn.execute("DROP TABLE IF EXISTS cycle_retrospective")
        # 知识库运营的表由它自己的读函数在首次使用时建出,这里每次从没有开始
        conn.execute("DROP TABLE IF EXISTS kb_ops_topic, kb_draft, kb_ops_run CASCADE")
    snapshot_store.reset_table_cache()
    report_store.reset_table_cache()
    kb_ops.reset_table_cache()


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


def _card(
    first_dept: str | None, *, hard_zero: bool = False, confidence: str | None = None
) -> dict:
    card = {
        "schema": "evaluation_scorecard/v2",
        "intended": {"first": first_dept, "second": None},
        "hard_zero": hard_zero,
        "total": None,
    }
    if confidence is not None:
        # 带批判者的评分模式出的卡才有这一块;单评委模式的卡没有
        card["panel"] = {"mode": "critic", "confidence": confidence}
    return card


def _scorecard(resume_id: int, first_dept: str | None, *, cycle_id: int = CYCLE,
               status: str = "draft", hard_zero: bool = False,
               confidence: str | None = None) -> int:
    version = ev_store.save_scorecard(
        _card(first_dept, hard_zero=hard_zero, confidence=confidence),
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


# ── 人机一致域聚合 ──────────────────────────────────────


def test_agreement_aggregates_read_confidence_from_the_decided_card() -> None:
    _scorecard(1, "技术部", status="rejected", confidence="low")
    _scorecard(2, "技术部", status="adopted", confidence="low")
    _scorecard(3, "媒体部", status="adopted", confidence="high")
    _scorecard(4, "媒体部", status="rejected", confidence="high")
    _scorecard(5, "项目部", status="rejected")  # 单评委模式的卡:没有置信度
    _scorecard(6, "项目部", confidence="low")  # 低置信但还没人看
    # 先被驳回(低置信),复评出了一张高置信的新 draft:人看的是被驳回的那张
    _scorecard(7, "综合部", status="rejected", confidence="low")
    _scorecard(7, "综合部", confidence="high")
    # 先驳回一张低置信的,后来采纳了一张高置信的:以最后一次决定所在的卡为准
    _scorecard(8, "综合部", status="rejected", confidence="low")
    _scorecard(8, "综合部", status="adopted", confidence="high")
    _scorecard(9, "技术部", cycle_id=OTHER_CYCLE, status="rejected", confidence="low")

    assert store.agreement_aggregates(CYCLE) == {
        "graded_decided": 6,  # 1 2 3 4 7 8;5 没有置信度,6 没有决定
        "low_confidence_decided": 3,  # 1 2 7
        "low_confidence_rejected": 2,  # 1 7
    }


def test_agreement_aggregates_on_a_cycle_scored_by_a_single_judge() -> None:
    _scorecard(1, "技术部", status="adopted")
    _scorecard(2, "技术部", status="rejected")

    agg = store.agreement_aggregates(CYCLE)

    assert agg == {"graded_decided": 0, "low_confidence_decided": 0, "low_confidence_rejected": 0}
    (absent,) = metrics.agreement_metrics(agg)
    assert isinstance(absent, metrics.Absent)  # 没有置信度可言,不出一个 0


# ── 知识库主题清单 ──────────────────────────────────────


def _kb_run(run_id: str, start: datetime, end: datetime, *topics, succeed: bool = True) -> None:
    kb_ops.create_run(run_id, window_start=start, window_end=end, triggered_by=7, params={})
    if not succeed:
        kb_ops.fail_run(run_id, "模型端点不可用")
        return
    rows = [
        kb_ops.TopicRow(
            rank=rank,
            query_count=queries,
            thread_count=threads,
            near_miss_count=near_miss,
            best_vec_score=0.4,
            first_seen=start,
            last_seen=end,
            sample_query_ids=(),
            triage_status=kb_ops.TRIAGE_DONE,
            scope=scope,
            topic=name,
            canonical_question=f"{name}是什么?",
            reason="测试",
        )
        for rank, (name, queries, threads, near_miss, scope) in enumerate(topics, start=1)
    ]
    kb_ops.finish_run(run_id, stats={}, prompt_versions={}, topics=rows)
    # 运行按开始时间排新旧;测试里一口气建出来的几次运行要拉开先后
    _execute("UPDATE kb_ops_run SET started_at = %s WHERE run_id = %s", (end, run_id))


def test_kb_topics_come_from_the_latest_successful_analysis_overlapping_the_window() -> None:
    day = timedelta(days=1)
    before = WINDOW.start - 40 * day
    _kb_run("old", before, before + 7 * day, ("往届的问题", 80, 60, 0, "official_knowledge"))
    _kb_run(
        "mid",
        WINDOW.start + 3 * day,
        WINDOW.start + 10 * day,
        ("招新报名截止时间", 30, 20, 0, "official_knowledge"),
    )
    _kb_run(
        "late",
        WINDOW.end - 5 * day,
        WINDOW.end + 2 * day,  # 伸到周期外面:有重叠就算
        ("招新报名截止时间", 120, 85, 20, "official_knowledge"),
        ("查询本人面试结果", 55, 41, 0, "personal_data"),
    )
    _kb_run("failed", WINDOW.end - 2 * day, WINDOW.end + 5 * day, succeed=False)
    _kb_run(
        "after",
        WINDOW.end + 10 * day,
        WINDOW.end + 17 * day,
        ("下一届的问题", 99, 70, 0, "official_knowledge"),
    )

    got = store.kb_topic_aggregates(WINDOW.start, WINDOW.end)

    assert got["run"] == {
        "run_id": "late",
        "window_start": WINDOW.end - 5 * day,
        "window_end": WINDOW.end + 2 * day,
    }
    assert [(t["topic"], t["query_count"], t["thread_count"]) for t in got["topics"]] == [
        ("招新报名截止时间", 120, 85),
        ("查询本人面试结果", 55, 41),
    ]
    # 经口径与抑制之后:只留该由知识库回答的那一个
    values = [v for item in metrics.kb_topic_metrics(got) for v in metrics.suppress(item)]
    assert [(v.id, v.label, v.value, v.group) for v in values] == [
        ("kb.top_topics[t01]", "招新报名截止时间", 120, "missing")
    ]


def test_kb_topics_window_bounds_are_half_open() -> None:
    """恰好在周期开始那一刻结束、或恰好在周期结束那一刻开始的分析,与周期没有重叠。"""
    week = timedelta(days=7)
    _kb_run("ends_at_start", WINDOW.start - week, WINDOW.start, ("甲", 9, 9, 0, None))
    _kb_run("starts_at_end", WINDOW.end, WINDOW.end + week, ("乙", 9, 9, 0, None))

    assert store.kb_topic_aggregates(WINDOW.start, WINDOW.end) is None


def test_kb_topics_on_a_database_that_never_ran_the_analysis() -> None:
    """知识库运营的表还没建过:读出来是「没有」,不是报错。"""
    assert not _table_exists("kb_ops_run")
    assert store.kb_topic_aggregates(WINDOW.start, WINDOW.end) is None


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
    # 评分段的用量行(tools 带 scoring):单独数,不算进题库段
    _turn(f"eval:{job_a}", channel="evaluation", input_tokens=4000, output_tokens=400,
          tools=["scoring"])
    _turn(f"eval:{job_a}", channel="evaluation", input_tokens=3000, output_tokens=300,
          tools=["scoring"])  # 重评:花掉的 token 照计
    _turn(f"eval:{job_b}", channel="evaluation", input_tokens=2000, output_tokens=200,
          tools=["scoring"])
    _turn(f"eval:{job_b}", channel="evaluation", tools=["scoring"])  # 模型没回用量
    _turn(f"eval:{job_other}", channel="evaluation", input_tokens=8000, output_tokens=800,
          tools=["scoring"])

    assert store.evaluation_usage_aggregates(CYCLE) == {
        "metered_resumes": 1,
        "input_tokens": 1500,
        "output_tokens": 150,
        "scoring_metered_resumes": 2,
        "scoring_input_tokens": 9000,
        "scoring_output_tokens": 900,
    }


def test_usage_rows_written_by_the_evaluation_runner_land_in_the_right_stage() -> None:
    """两段靠用量行上的段标记区分:标记由评估任务写、由这里读,两头必须对得上。"""
    from official_agent.evaluation import runner

    job_ids = [_job(resume_id) for resume_id in range(1, 7)]
    for job_id in job_ids:
        runner._write_eval_usage_log(
            job_id=job_id,
            job_user_id=None,
            stage_tools=runner._SCORING_STAGE_TOOLS,
            user_message="初筛评分",
            reply_summary="评分卡落库",
            usage={"input_tokens": 12_000, "output_tokens": 800},
        )
        runner._write_eval_usage_log(
            job_id=job_id,
            job_user_id=None,
            stage_tools=runner._QBANK_STAGE_TOOLS,
            user_message="出题",
            reply_summary="题库落库",
            usage={"input_tokens": 200_000, "output_tokens": 9_000},
        )

    usage = store.evaluation_usage_aggregates(CYCLE)

    assert usage == {
        "metered_resumes": 6,
        "input_tokens": 1_200_000,
        "output_tokens": 54_000,
        "scoring_metered_resumes": 6,
        "scoring_input_tokens": 72_000,
        "scoring_output_tokens": 4_800,
    }
    conversation_agg = store.conversation_aggregates(WINDOW.start, WINDOW.end)
    values = {
        v.id: v
        for item in metrics.cost_metrics(conversation_agg, usage)
        for v in metrics.suppress(item)
    }
    assert (values["cost.scoring_tokens"].value, values["cost.scoring_tokens"].n) == (76_800, 6)
    assert values["cost.evaluation_tokens"].value == 1_254_000  # 题库段没有被评分段的行污染


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


# ── 报告表:自举与状态机 ────────────────────────────────


def _start(cycle_id: int = CYCLE, **over) -> int:
    fields = {
        "cycle_id": cycle_id,
        "metrics_schema": catalog.METRICS_SCHEMA,
        "snapshot_version": 1,
        "baseline_cycle_id": None,
        "baseline_snapshot_version": None,
        "created_by": 7,
    }
    fields.update(over)
    return report_store.start_report(**fields)


def _finish(version: int, *, cycle_id: int = CYCLE, marker: str = "正文") -> bool:
    return report_store.finish_report(
        cycle_id,
        version,
        sections={"summary": {"text": f"{marker}:技术部", "metric_ids": ["funnel.resumes_total"]}},
        report_md=f"# 报告 {marker}",
        unverified=[{"where": "summary", "text": "删掉的句子", "reason": "对不上"}],
        prompt_versions={"writer": "retrospective_writer/v1"},
        usage={"input_tokens": 400, "output_tokens": 40},
    )


def _draft(cycle_id: int = CYCLE, marker: str = "正文") -> int:
    version = _start(cycle_id)
    assert _finish(version, cycle_id=cycle_id, marker=marker)
    return version


def _statuses(cycle_id: int = CYCLE) -> dict[int, str]:
    return {item["version"]: item["status"] for item in report_store.list_reports(cycle_id)}


def test_report_table_is_created_on_a_database_that_never_had_it() -> None:
    assert not _table_exists("cycle_retrospective")
    assert report_store.get_report(CYCLE) is None  # 读也能自举,不报表不存在
    assert _table_exists("cycle_retrospective")
    report_store.reset_table_cache()
    report_store.ensure_report_table()  # 再跑一遍无害


def test_report_goes_from_generating_to_draft_with_its_content() -> None:
    version = _start(baseline_cycle_id=11, baseline_snapshot_version=4, snapshot_version=2)
    reserved = report_store.get_report(CYCLE)
    assert (reserved.version, reserved.status) == (1, report_store.STATUS_GENERATING)
    assert (reserved.snapshot_version, reserved.baseline_cycle_id) == (2, 11)
    assert reserved.baseline_snapshot_version == 4
    assert reserved.sections == {} and reserved.report_md == "" and reserved.finished_at is None

    assert _finish(version) is True

    draft = report_store.get_report(CYCLE, version=version)
    assert draft.status == report_store.STATUS_DRAFT
    assert draft.sections["summary"]["text"] == "正文:技术部"  # jsonb 往返,中文原样
    assert draft.report_md == "# 报告 正文"
    assert draft.unverified[0]["reason"] == "对不上"
    assert draft.prompt_versions == {"writer": "retrospective_writer/v1"}
    assert draft.usage == {"input_tokens": 400, "output_tokens": 40}
    assert draft.created_by == 7 and draft.finished_at is not None
    assert draft.published_by is None and draft.error is None


def test_only_one_report_per_cycle_generates_at_a_time() -> None:
    _start()
    with pytest.raises(report_store.ReportInProgressError):
        _start()
    assert _start(OTHER_CYCLE) == 1  # 别的周期不受影响,版本号各自计


def test_finished_or_failed_generation_frees_the_slot() -> None:
    first = _start()
    assert report_store.fail_report(CYCLE, first, "生成失败(RuntimeError)") is True
    failed = report_store.get_report(CYCLE, version=first)
    assert (failed.status, failed.error) == (report_store.STATUS_FAILED, "生成失败(RuntimeError)")

    second = _start()
    assert second == 2
    assert _finish(second)
    assert _start() == 3
    assert _statuses() == {1: "failed", 2: "draft", 3: "generating"}


def test_result_is_written_only_while_the_version_is_still_generating() -> None:
    version = _draft()
    assert _finish(version, marker="迟到的结果") is False  # 已经是草稿,不被覆盖
    assert report_store.fail_report(CYCLE, version, "x") is False
    assert report_store.get_report(CYCLE, version=version).report_md == "# 报告 正文"


def test_generation_abandoned_by_a_dead_process_is_cleared_on_the_next_start() -> None:
    stuck = _start()
    _execute(
        "UPDATE cycle_retrospective SET created_at = now() - interval '20 minutes' "
        "WHERE cycle_id = %s AND version = %s",
        (CYCLE, stuck),
    )

    fresh = _start()

    assert fresh == 2
    abandoned = report_store.get_report(CYCLE, version=stuck)
    assert (abandoned.status, abandoned.error) == ("failed", "生成超时或进程中断")
    assert _finish(stuck, marker="迟到") is False  # 被判失败之后迟到的结果不写进去
    assert report_store.get_report(CYCLE, version=stuck).report_md == ""


def test_abandoned_generation_shows_as_failed_to_whoever_is_polling() -> None:
    """轮询的人不该对着一个永远不会完成的 generating 一直等。"""
    stuck = _start()
    _execute(
        "UPDATE cycle_retrospective SET created_at = now() - interval '20 minutes' "
        "WHERE cycle_id = %s AND version = %s",
        (CYCLE, stuck),
    )
    seen = report_store.get_report(CYCLE, version=stuck)
    assert (seen.status, seen.error) == ("failed", "生成超时或进程中断")
    assert _statuses() == {1: "failed"}


def test_recent_generation_is_not_mistaken_for_abandoned() -> None:
    _start()
    _execute(
        "UPDATE cycle_retrospective SET created_at = now() - interval '5 minutes' "
        "WHERE cycle_id = %s",
        (CYCLE,),
    )
    with pytest.raises(report_store.ReportInProgressError):
        _start()
    assert report_store.get_report(CYCLE).status == "generating"  # 读取也不会误判


def test_concurrent_starts_reserve_exactly_one_version() -> None:
    report_store.ensure_report_table()
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=5)
            outcomes.append(_start())
        except Exception as exc:  # noqa: BLE001 — 收集后在主线程断言
            outcomes.append(type(exc))

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert sorted(outcomes, key=str) == [1, report_store.ReportInProgressError]


# ── 报告表:发布 ────────────────────────────────────────


def test_publishing_a_draft_and_then_superseding_it() -> None:
    first = _draft(marker="第一版")
    published = report_store.publish_report(CYCLE, first, published_by=42)
    assert (published.status, published.published_by) == ("published", 42)
    assert published.published_at is not None
    assert published.report_md == "# 报告 第一版"  # 发布不改正文

    second = _draft(marker="第二版")
    report_store.publish_report(CYCLE, second, published_by=43)

    assert _statuses() == {1: "superseded", 2: "published"}
    old = report_store.get_report(CYCLE, version=first)
    assert (old.published_by, old.report_md) == (42, "# 报告 第一版")  # 被取代的版本原样保留


@pytest.mark.parametrize("state", ["generating", "failed", "published", "superseded"])
def test_only_a_draft_can_be_published(state: str) -> None:
    version = _start()
    if state == "failed":
        report_store.fail_report(CYCLE, version, "x")
    elif state in ("published", "superseded"):
        _finish(version)
        report_store.publish_report(CYCLE, version, published_by=1)
        if state == "superseded":
            report_store.publish_report(CYCLE, _draft(), published_by=1)

    with pytest.raises(report_store.ReportNotPublishableError) as info:
        report_store.publish_report(CYCLE, version, published_by=2)

    assert info.value.status == state
    assert report_store.get_report(CYCLE, version=version).status == state  # 没有被改动


def test_publishing_a_missing_version_raises() -> None:
    _draft()
    with pytest.raises(LookupError):
        report_store.publish_report(CYCLE, 9, published_by=1)
    with pytest.raises(LookupError):
        report_store.publish_report(OTHER_CYCLE, 1, published_by=1)


def test_concurrent_publishes_leave_exactly_one_published() -> None:
    first, second = _draft(marker="甲"), _draft(marker="乙")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _worker(version: int) -> None:
        try:
            barrier.wait(timeout=5)
            report_store.publish_report(CYCLE, version, published_by=1)
        except Exception as exc:  # noqa: BLE001 — 收集后在主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(v,)) for v in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    assert sorted(_statuses().values()) == ["published", "superseded"]


def test_database_itself_refuses_a_second_published_report() -> None:
    """应用层的锁之外的最后一道闸:绕过存储层直接改库也出不了两份已发布。"""
    report_store.publish_report(CYCLE, _draft(), published_by=1)
    second = _draft()
    with pytest.raises(psycopg.errors.UniqueViolation):
        _execute(
            "UPDATE cycle_retrospective SET status = 'published' "
            "WHERE cycle_id = %s AND version = %s",
            (CYCLE, second),
        )


def test_readers_without_review_rights_see_only_the_published_version() -> None:
    first = _draft(marker="已发布")
    report_store.publish_report(CYCLE, first, published_by=1)
    _draft(marker="新草稿")
    _start()

    assert report_store.get_report(CYCLE).status == "generating"  # 审阅者看到最新一版
    visible = report_store.get_report(CYCLE, published_only=True)
    assert (visible.version, visible.report_md) == (first, "# 报告 已发布")
    assert report_store.get_report(CYCLE, version=2, published_only=True) is None
    assert [i["version"] for i in report_store.list_reports(CYCLE)] == [3, 2, 1]
    listed = report_store.list_reports(CYCLE, published_only=True)
    assert [i["version"] for i in listed] == [first]
    assert "sections" not in listed[0] and "report_md" not in listed[0]


# ── 报告:真库上生成、经接口发布 ────────────────────────


@pytest.mark.asyncio
async def test_generate_report_end_to_end_on_real_database() -> None:
    from tests.retro_helpers import ScriptedModel, finding, item, make_snapshot

    from official_agent.retrospective import reports

    snapshot = await make_snapshot(cycle_id=CYCLE)
    model = ScriptedModel(
        {
            "qa": {"findings": [finding("未命中率 18.00%。", "kb.not_found_rate")]},
            "writer": {
                "summary": item("未命中率 18%。另有 7 次超时。", "kb.not_found_rate"),
                "sections": [
                    {"role": "qa", "items": [item("未命中率 18%。", "kb.not_found_rate")]}
                ],
            },
        }
    )
    version = _start()

    await reports.generate_report(cycle_id=CYCLE, version=version, snapshot=snapshot, model=model)

    draft = report_store.get_report(CYCLE, version=version)
    assert draft.status == report_store.STATUS_DRAFT
    assert draft.sections["summary"]["text"] == "未命中率 18%。"
    assert draft.sections["sections"][1]["items"][0]["text"] == "未命中率 18%。"
    assert [entry["text"] for entry in draft.unverified] == ["另有 7 次超时。"]
    assert draft.report_md.startswith(f"# 招新周期复盘报告 · 周期 {CYCLE}(第 {version} 版)")
    assert draft.prompt_versions["qa"] == "retrospective_qa/v2"


def test_report_api_roundtrip_on_real_database(backend, monkeypatch, web_no_real_pg) -> None:
    """走真实路由:生成(后台)→ 审阅者看到草稿 → 发布 → 只有查看权的人看到已发布版。"""
    import contextlib
    import time

    from fastapi.testclient import TestClient
    from tests.retro_helpers import ScriptedModel, finding, item

    from official_agent.config import get_settings
    from official_agent.web.app import create_app

    @contextlib.asynccontextmanager
    async def _no_checkpointer():
        yield None

    codes = {"tok-admin": ["evaluation:run", "resume:audit"], "tok-reviewer": ["resume:audit"]}

    async def _resolve(credentials, *_a, **_k):
        return {
            "user_id": 42 if credentials["token"] == "tok-admin" else 9,
            "role": "admin",
            "role_names": ["管理员"],
            "permission_codes": codes[credentials["token"]],
            "source": "web",
        }

    model = ScriptedModel(
        {
            "data": {"findings": [finding("共投递 40 份。", "funnel.resumes_total")]},
            "writer": {
                "sections": [
                    {"role": "data", "items": [item("共投递 40 份。", "funnel.resumes_total")]}
                ]
            },
        }
    )
    audits: list[dict] = []
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _no_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    monkeypatch.setattr("official_agent.graphs.assistant.build_model", lambda *_a, **_k: model)
    monkeypatch.setattr(get_settings(), "retrospective_report_enabled", True)
    monkeypatch.setattr(
        "official_agent.state.audit.write_audit", lambda **kwargs: audits.append(kwargs)
    )
    for i in range(6):
        _turn(f"web:u{i}", duration_ms=100, tools=["search_knowledge"])
    admin = {"Authorization": "Bearer tok-admin"}
    reviewer = {"Authorization": "Bearer tok-reviewer"}
    base = "/api/agent/admin/retrospective"

    with TestClient(create_app()) as client:
        started = client.post(f"{base}/run", json={"cycle_id": CYCLE}, headers=admin)
        assert started.status_code == 202, started.text
        assert started.json() == {
            "cycle_id": CYCLE,
            "version": 1,
            "status": "generating",
            "snapshot_version": 1,
            "baseline_cycle_id": None,
        }
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            report = client.get(f"{base}/report", params={"cycle_id": CYCLE}, headers=admin)
            if report.json().get("status") != "generating":
                break
            time.sleep(0.05)
        hidden = client.get(f"{base}/report", params={"cycle_id": CYCLE}, headers=reviewer)
        published = client.post(
            f"{base}/publish", json={"cycle_id": CYCLE, "version": 1}, headers=admin
        )
        visible = client.get(f"{base}/report", params={"cycle_id": CYCLE}, headers=reviewer)
        listed = client.get(f"{base}/reports", params={"cycle_id": CYCLE}, headers=reviewer)

    body = report.json()
    assert body["status"] == "draft", body
    assert body["sections"]["sections"][0]["items"][0]["text"] == "共投递 40 份。"
    assert body["created_by"] == 42 and body["snapshot_version"] == 1
    assert hidden.status_code == 404  # 草稿对只有查看权的人不存在
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert published.json()["published_by"] == 42
    assert visible.status_code == 200 and visible.json()["status"] == "published"
    assert visible.json()["report_md"] == body["report_md"]
    assert [i["version"] for i in listed.json()["items"]] == [1]
    assert [a["action"]["op"] for a in audits] == ["publish_retrospective"]
    assert audits[0]["acting_user_id"] == 42
