"""回复忠实度抽样的真库集成档:抽样、级联删除、端到端运行、经管理面触发。

运行门槛与 test_kb_integration.py 相同:KB_TEST_DATABASE_URL 指向一次性库(带 pgvector),
缺省整档跳过。核对角色用写死的假实现,嵌入用二维假向量。

⚠️ 本档会清空并重建 kb_faithfulness_run / kb_faithfulness_sample,只能指向一次性库;
指到 agent 服务库(official_agent)时整档跳过。
"""

import contextlib
import os
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

pytestmark = pytest.mark.skipif(
    not os.environ.get("KB_TEST_DATABASE_URL"),
    reason="需要 KB_TEST_DATABASE_URL(本地 pgvector 容器里的一次性库)",
)

from official_agent.config import Settings  # noqa: E402
from official_agent.kb import query_log  # noqa: E402
from official_agent.kb import store as kb_store  # noqa: E402
from official_agent.kb.store import SourceInput  # noqa: E402
from official_agent.kb_ops import faithfulness_runner  # noqa: E402
from official_agent.kb_ops.faithfulness import FaithfulnessOutput  # noqa: E402
from official_agent.kb_ops.faithfulness_runner import RunRequest  # noqa: E402
from official_agent.state import conversation  # noqa: E402
from official_agent.state import kb_faithfulness as state  # noqa: E402
from official_agent.state.kb_faithfulness import SampleRow  # noqa: E402

_PROD_DB_NAME = "official_agent"
_ACTOR = "it-kbfaith"
_FEE_TEXT = "本社团不向成员收取会费,日常活动经费由学院拨款和赞助支持。"


def _url() -> str:
    return os.environ["KB_TEST_DATABASE_URL"]


async def _embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] for _ in texts]


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with psycopg.connect(_url(), row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(sql: str, params: tuple = ()) -> None:
    with psycopg.connect(_url()) as conn:
        conn.execute(sql, params)
        conn.commit()


class _Talk:
    """往对话日志与检索日志里放合成的对话轮。每个实例一段专属的历史时间窗与会话前缀。"""

    def __init__(self) -> None:
        self.tag = secrets.token_hex(4)
        self.start = datetime(2001, 1, 1, tzinfo=UTC) + timedelta(days=secrets.randbelow(6900))
        self.end = self.start + timedelta(days=7)
        self._minute = 0

    def thread(self, name: str) -> str:
        return f"it:kbfaith:{self.tag}:{name}"

    def turn(
        self,
        thread: str,
        question: str,
        reply: str,
        *,
        hits: list[str] | None,
        error_code: str | None = None,
        guard_verdict: str | None = None,
        cycle_ids: list[int] | None = None,
    ) -> int:
        """一轮对话:对话日志一行;hits 不为 None 时再记一次知识库检索(空表 = 没命中)。"""
        self._minute += 1
        at = self.start + timedelta(minutes=self._minute)
        trace = secrets.token_hex(16)
        with psycopg.connect(_url()) as conn:
            row = conn.execute(
                """
                INSERT INTO agent_conversation_log
                    (thread_id, user_id, user_message, reply_summary, error_code, trace_id,
                     guard_verdict, created_at)
                VALUES (%s, 7, %s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (self.thread(thread), question, reply, error_code, trace, guard_verdict, at),
            ).fetchone()
            if hits is not None:
                conn.execute(
                    """
                    INSERT INTO kb_query_log
                        (thread_id, trace_id, query, status, hit_source_ids, cycle_ids, created_at)
                    VALUES (%s, %s, %s, %s, %s::text[], %s::int[], %s)
                    """,
                    (
                        self.thread(thread),
                        trace,
                        question,
                        "ok" if hits else "not_found",
                        hits,
                        cycle_ids or [],
                        at,
                    ),
                )
            conn.commit()
        return int(row[0])

    def cleanup(self) -> None:
        pattern = f"it:kbfaith:{self.tag}:%"
        _execute("DELETE FROM agent_conversation_log WHERE thread_id LIKE %s", (pattern,))
        _execute("DELETE FROM kb_query_log WHERE thread_id LIKE %s", (pattern,))


@pytest.fixture()
def faith_db(monkeypatch) -> Settings:
    dbname = psycopg.conninfo.conninfo_to_dict(_url()).get("dbname") or ""
    if dbname == _PROD_DB_NAME:
        pytest.skip(f"拒绝在 agent 服务库 {_PROD_DB_NAME!r} 上跑本档:会清空并重建抽样表")
    settings = Settings(
        _env_file=None,
        postgres_url=_url(),
        embed_model="it-fake-model",
        embed_dim=2,
        kb_ops_faithfulness_enabled=True,  # 默认关闭,本档验证的正是打开之后的行为
    )
    for module in (
        "state.kb_faithfulness",
        "state.pools",
        "state.conversation",
        "state.audit",
        "kb.query_log",
        "kb.store",
        "kb.schema",
    ):
        monkeypatch.setattr(f"official_agent.{module}.get_settings", lambda: settings)
    monkeypatch.setattr(faithfulness_runner, "get_effective_settings", lambda: settings)
    monkeypatch.setattr("official_agent.kb.store.embed_texts", _embed)
    state.reset_table_cache()
    query_log.reset_table_cache()
    kb_store.reset_schema_cache()
    state.ensure_faithfulness_tables()
    _execute("TRUNCATE kb_faithfulness_run CASCADE")
    # 上一次被中断的测试进程可能留下合成的对话与检索日志;它们落在随机的历史时间窗里,
    # 会混进别的用例的统计。测试是串行的,此刻不会有别的用例在用这些行
    _execute("DELETE FROM agent_conversation_log WHERE thread_id LIKE 'it:kb%%'")
    _execute("DELETE FROM kb_query_log WHERE thread_id LIKE 'it:kb%%'")
    _execute("DELETE FROM kb_source WHERE updated_by = %s", (_ACTOR,))
    yield settings
    _execute("DELETE FROM kb_source WHERE updated_by = %s", (_ACTOR,))


@pytest.fixture()
def talk(faith_db) -> Any:
    talk = _Talk()
    yield talk
    talk.cleanup()


def _new_run(run_id: str, *, started_at: datetime | None = None) -> None:
    now = datetime.now(UTC)
    state.create_run(
        run_id, window_start=now - timedelta(days=7), window_end=now, triggered_by=2, params={}
    )
    if started_at is not None:
        _execute(
            "UPDATE kb_faithfulness_run SET started_at = %s WHERE run_id = %s", (started_at, run_id)
        )


# ── 表与锁 ──────────────────────────────────────────────


def test_tables_are_created_on_a_database_without_them(faith_db) -> None:
    """存量库没有抽样的两张表:首次使用时建出来(连同它依赖的对话日志表),再自举不动数据。"""
    _execute("DROP TABLE kb_faithfulness_sample")
    _execute("DROP TABLE kb_faithfulness_run")
    state.reset_table_cache()

    assert state.list_runs() == []
    _new_run("kbfaith_keep")
    state.finish_run("kbfaith_keep", stats={"faithfulness": 0.9}, prompt_versions={}, samples=[])
    state.reset_table_cache()
    state.ensure_faithfulness_tables()
    run = state.latest_succeeded_run()
    assert run["run_id"] == "kbfaith_keep" and run["stats"] == {"faithfulness": 0.9}


def test_run_lock_and_leftover_running_rows(faith_db) -> None:
    first = state.try_acquire_run_lock()
    assert first is not None
    try:
        assert state.try_acquire_run_lock() is None
    finally:
        first.close()
    _new_run("kbfaith_crashed")
    _new_run("kbfaith_next")
    crashed = state.get_run("kbfaith_crashed")
    assert (crashed["status"], crashed["error"]) == ("failed", "进程中断,运行没有完成")
    state.fail_run("kbfaith_next", "运行失败,详情见服务日志")
    assert state.get_run("kbfaith_next")["error"] == "运行失败,详情见服务日志"
    assert state.latest_succeeded_run() is None


# ── 抽样 ────────────────────────────────────────────────


def test_candidates_are_answered_turns_with_a_kb_hit(talk) -> None:
    good = talk.turn("u1", "入社要交钱吗", "不用。", hits=["kb_a", "kb_b"], cycle_ids=[12])
    also = talk.turn("u2", "面试在哪", "信息楼。", hits=["kb_c"])
    talk.turn("u3", "社团有奖学金吗", "知识库里没有。", hits=[])  # 检索了但没命中
    talk.turn("u4", "你好", "你好!", hits=None)  # 没检索知识库
    talk.turn("u5", "入社要交钱吗", "", hits=["kb_a"], error_code="timeout")  # 失败的轮次
    talk.turn("u6", "入社要交钱吗", "", hits=["kb_a"])  # 没有回复

    window = {"since": talk.start, "until": talk.end}
    assert state.count_candidates(**window) == 2
    samples = state.sample_candidates(seed="s1", limit=10, **window)
    by_id = {s["conversation_id"]: s for s in samples}
    assert set(by_id) == {good, also}
    assert by_id[good]["source_ids"] == ["kb_a", "kb_b"] and by_id[good]["cycle_ids"] == [12]
    assert (
        by_id[good]["user_message"] == "入社要交钱吗" and by_id[good]["reply_summary"] == "不用。"
    )
    assert by_id[also]["source_ids"] == ["kb_c"] and by_id[also]["cycle_ids"] == []

    # 同一个种子抽到同一批、同一个顺序;limit 生效
    again = state.sample_candidates(seed="s1", limit=10, **window)
    assert [s["conversation_id"] for s in again] == [s["conversation_id"] for s in samples]
    assert len(state.sample_candidates(seed="s1", limit=1, **window)) == 1
    outside = {"since": talk.start - timedelta(days=30), "until": talk.start - timedelta(days=29)}
    assert state.count_candidates(**outside) == 0
    assert state.sample_candidates(seed="s1", limit=10, **outside) == []


def test_sampling_seed_changes_the_order(talk) -> None:
    for i in range(12):
        talk.turn(f"u{i}", f"问题{i}", "答。", hits=["kb_a"])
    window = {"since": talk.start, "until": talk.end}
    orders = {
        tuple(s["conversation_id"] for s in state.sample_candidates(seed=seed, limit=12, **window))
        for seed in ("a", "b", "c", "d")
    }
    assert len(orders) > 1  # 不是固定按时间或 id 取前几条


def test_citation_verdicts_are_counted_from_the_conversation_log(talk) -> None:
    for verdict in (
        "kb_citation=clean",
        "fabrication_empty_tools=clean;kb_citation=clean",
        "kb_citation=unverified:奖学金办法",
        "kb_citation=unsearched:技术部介绍",
        "fabrication_empty_tools=clean",  # 没有引用核验这一项
        "triggered:查询结果",  # 旧格式的编造守卫判定
        None,
    ):
        talk.turn("u1", "问", "答。", hits=None, guard_verdict=verdict)
    counts = state.summarize_citation_verdicts(since=talk.start, until=talk.end)
    assert counts == {"clean": 2, "unverified": 1, "unsearched": 1}


# ── 样本的留存:随运行删除,随会话删除 ───────────────────────


def test_samples_cascade_with_the_conversation_and_with_the_run(talk) -> None:
    keep = talk.turn("u1", "入社要交钱吗", "不用。", hits=["kb_a"])
    gone = talk.turn("u2", "面试在哪", "信息楼。", hits=["kb_b"])
    vanished = talk.turn("u3", "社团做什么", "写代码。", hits=["kb_c"])
    # 抽样之后、落库之前,u3 的用户删了会话
    conversation.delete_thread_conversations(talk.thread("u3"))

    _new_run("kbfaith_c1", started_at=datetime(2026, 1, 1, tzinfo=UTC))
    claims = ({"text": "加入社团不收会费", "verdict": "supported", "reason": ""},)
    state.finish_run(
        "kbfaith_c1",
        stats={},
        prompt_versions={"faithfulness": "v1"},
        samples=[
            SampleRow(keep, "judged", ("kb_a",), 1, 1, claims=claims),
            SampleRow(gone, "judged", ("kb_b",), 2, 0, 1, 1),
            SampleRow(vanished, "judged", ("kb_c",), 1, 1),
        ],
    )
    samples = state.list_samples("kbfaith_c1")
    # 对话已不在的那个样本没有落库,其余照常;有问题的排前面;问题与回复是现读的
    assert [s["conversation_id"] for s in samples] == [gone, keep]
    assert samples[0]["user_message"] == "面试在哪" and samples[0]["contradicted"] == 1
    assert samples[1]["claims"] == list(claims) and samples[1]["source_ids"] == ["kb_a"]
    assert state.get_run("kbfaith_c1")["status"] == "succeeded"

    # 用户删会话:对话日志一删,样本跟着没了(没有在删除路径上另接一步)
    assert conversation.delete_thread_conversations(talk.thread("u2")) == 1
    assert [s["conversation_id"] for s in state.list_samples("kbfaith_c1")] == [keep]

    # 只留最近一次运行:旧运行连同样本删除,对话日志不受影响
    _new_run("kbfaith_c2", started_at=datetime(2026, 1, 2, tzinfo=UTC))
    state.finish_run("kbfaith_c2", stats={}, prompt_versions={}, samples=[])
    assert state.purge_old_runs(1) == 1
    assert [r["run_id"] for r in state.list_runs()] == ["kbfaith_c2"]
    assert _rows("SELECT 1 FROM kb_faithfulness_sample") == []
    assert _rows("SELECT 1 FROM agent_conversation_log WHERE id = %s", (keep,))


def test_sample_constraints(talk) -> None:
    turn = talk.turn("u1", "问", "答。", hits=["kb_a"])
    _new_run("kbfaith_k")
    with pytest.raises(psycopg.errors.CheckViolation):
        state.finish_run(
            "kbfaith_k", stats={}, prompt_versions={}, samples=[SampleRow(turn, "guessed")]
        )
    with pytest.raises(psycopg.errors.UniqueViolation):
        state.finish_run(
            "kbfaith_k",
            stats={},
            prompt_versions={},
            samples=[SampleRow(turn, "judged"), SampleRow(turn, "failed")],
        )
    assert state.list_samples("kbfaith_k") == []  # 整体回滚
    assert state.get_run("kbfaith_k")["status"] == "running"


# ── 端到端 ──────────────────────────────────────────────


async def _seed_kb() -> str:
    return await kb_store.ingest_source(
        SourceInput(
            title="会费说明",
            type="faq",
            question="加入社团需要交费吗?",
            answer=_FEE_TEXT,
            updated_by=_ACTOR,
        ),
        embedder=_embed,
    )


async def _judge(question: str, reply: str, chunks) -> Any:
    """按回复内容给出写死的判断;引文取自真正读到的那一块。"""
    chunk_id = chunks[0].chunk_id
    if "不向成员收取会费" in reply:
        claims = [
            {
                "text": "不收会费",
                "verdict": "supported",
                "chunk_id": chunk_id,
                "quote": "本社团不向成员收取会费",
            }
        ]
    elif "要缴纳" in reply:
        claims = [
            {
                "text": "要缴纳会费",
                "verdict": "contradicted",
                "chunk_id": chunk_id,
                "quote": "本社团不向成员收取会费",
            }
        ]
    else:
        # 判为有依据,但引文是编的:代码复核后不算
        claims = [
            {
                "text": "每学期有补贴",
                "verdict": "supported",
                "chunk_id": chunk_id,
                "quote": "每学期发放活动补贴",
            }
        ]
    usage = {"input_tokens": 800, "output_tokens": 60}
    return FaithfulnessOutput.model_validate({"claims": claims}), usage


def _seed_talk(talk: _Talk, source_id: str) -> dict[str, int]:
    return {
        "faithful": talk.turn(
            "u1", "入社要交钱吗", "不用,本社团不向成员收取会费。", hits=[source_id]
        ),
        "contradicting": talk.turn("u2", "入社要交钱吗", "要缴纳会费。", hits=[source_id]),
        "invented": talk.turn("u3", "社团有补贴吗", "每学期有补贴。", hits=[source_id]),
        "stale": talk.turn("u4", "面试在哪", "在信息楼。", hits=["kb_deleted_since"]),
    }


async def test_execute_run_end_to_end(talk) -> None:
    source_id = await _seed_kb()
    ids = _seed_talk(talk, source_id)
    talk.turn("u5", "见《会费说明》", "好的。", hits=None, guard_verdict="kb_citation=clean")
    request = RunRequest(
        run_id=f"kbfaith_{talk.tag}",
        window_start=talk.start,
        window_end=talk.end,
        sample_size=30,
        triggered_by=2,
    )
    state.create_run(
        request.run_id,
        window_start=request.window_start,
        window_end=request.window_end,
        triggered_by=2,
        params={"days": 7, "sample_size": 30},
    )

    await faithfulness_runner.execute_run(request, judge=_judge)  # 证据走真实的知识库读取

    run = state.latest_succeeded_run()
    assert run["run_id"] == request.run_id and run["error"] is None
    assert run["prompt_versions"] == {"faithfulness": "kb_ops_faithfulness/v1"}
    assert run["stats"] == {
        "candidates": 4,
        "sampled": 4,
        "samples": {"judged": 3, "no_evidence": 1},
        "claims": 3,
        "supported": 1,
        "unsupported": 1,
        "contradicted": 1,
        "faithfulness": 0.3333,
        "turns_with_claims": 3,
        "fully_supported_turns": 1,
        "turns_with_contradiction": 1,
        "budget_exhausted": False,
        "citation": {"clean": 1, "unverified": 0, "unsearched": 0},
        "input_tokens": 2400,
        "output_tokens": 180,
    }
    samples = {s["conversation_id"]: s for s in state.list_samples(request.run_id)}
    assert samples[ids["faithful"]]["claims"][0]["chunk_id"] == f"{source_id}#0"
    assert samples[ids["contradicting"]]["contradicted"] == 1
    invented = samples[ids["invented"]]
    assert (invented["supported"], invented["unsupported"]) == (0, 1)
    assert invented["claims"][0]["reason"] == "判为有依据,但给的引文在原文里找不到"
    stale = samples[ids["stale"]]
    assert stale["status"] == "no_evidence" and stale["claims_total"] == 0


def test_admin_api_trigger_and_read_real_pg(talk, monkeypatch, web_no_real_pg) -> None:
    import asyncio

    from fastapi.testclient import TestClient

    from official_agent.web.app import create_app

    source_id = asyncio.run(_seed_kb())
    ids = _seed_talk(talk, source_id)

    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield object()

    async def _resolve(*_a: object, **_k: object) -> dict:
        return {"user_id": 2, "role": "admin", "permission_codes": ["kb:manage"], "source": "web"}

    class _FrozenNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return talk.end

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    monkeypatch.setattr(faithfulness_runner, "datetime", _FrozenNow)
    monkeypatch.setattr(faithfulness_runner, "_default_judge", lambda _settings: _judge)
    auth = {"Authorization": "Bearer tok"}
    runs_url = "/api/agent/admin/kb/ops/faithfulness/runs"
    samples_url = "/api/agent/admin/kb/ops/faithfulness/samples"

    with TestClient(create_app()) as client:
        empty = client.get(samples_url, headers=auth)
        assert empty.status_code == 200 and empty.json() == {"run": None, "items": [], "total": 0}

        started = client.post(runs_url, headers=auth, json={"days": 7, "sample_size": 10})
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        deadline = time.monotonic() + 10
        while True:
            runs = client.get(runs_url, headers=auth).json()["items"]
            status = next(r["status"] for r in runs if r["run_id"] == run_id)
            if status != "running" or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert status == "succeeded"
        run = next(r for r in runs if r["run_id"] == run_id)
        assert run["stats"]["faithfulness"] == 0.3333 and run["triggered_by"] == 2
        assert run["params"] == {"days": 7, "sample_size": 10}

        data = client.get(samples_url, headers=auth).json()
        assert data["run"]["run_id"] == run_id and data["total"] == 4
        # 有问题的排前面:矛盾的那轮第一
        assert data["items"][0]["conversation_id"] == ids["contradicting"]
        assert data["items"][0]["reply_summary"] == "要缴纳会费。"
        problems = client.get(f"{samples_url}?problems_only=true", headers=auth).json()
        assert {s["conversation_id"] for s in problems["items"]} == {
            ids["contradicting"],
            ids["invented"],
        }

        # 用户删了会话:那一轮的样本从清单里消失
        conversation.delete_thread_conversations(talk.thread("u2"))
        after = client.get(samples_url, headers=auth).json()
        assert ids["contradicting"] not in {s["conversation_id"] for s in after["items"]}
        assert after["total"] == 3


def test_admin_api_refuses_while_the_switch_is_off(faith_db, monkeypatch, web_no_real_pg) -> None:
    from fastapi.testclient import TestClient

    from official_agent.web.app import create_app

    off = faith_db.model_copy(update={"kb_ops_faithfulness_enabled": False})
    monkeypatch.setattr(faithfulness_runner, "get_effective_settings", lambda: off)

    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield object()

    async def _resolve(*_a: object, **_k: object) -> dict:
        return {"user_id": 2, "role": "admin", "permission_codes": ["kb:manage"], "source": "web"}

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/agent/admin/kb/ops/faithfulness/runs",
            headers={"Authorization": "Bearer tok"},
            json={},
        )
        assert resp.status_code == 409 and "未开启" in resp.json()["detail"]
    assert state.list_runs() == []  # 没有登记运行,也没有抢锁
    lock = state.try_acquire_run_lock()
    assert lock is not None
    lock.close()
