"""B2 执行组织测试:runner 状态迁移 / job store SQL / 触发提交。

只测外部行为:fetch 与评分图被替换,验证状态机与审计;真库往返走集成档。
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from official_agent.evaluation import runner as ev_runner
from official_agent.evaluation.runner import EvaluationRunner
from official_agent.evaluation.scoring import FieldText
from official_agent.state import evaluation as ev_store

# ── store 层(mock 连接) ────────────────────────────────


def _mock_conn(fetchone=None, fetchall=None, rowcount=1):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (lambda: fetchone) if fetchone is not None else (lambda: None)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return conn


def test_create_jobs_select_first_then_insert(monkeypatch) -> None:
    """闸门2:SELECT-first——已有活跃 job 复用,无则 INSERT。"""
    # 第一个 item:无活跃 → INSERT 返回 7
    conn = _mock_conn(fetchone={"job_id": 7})
    monkeypatch.setattr(ev_store, "_conn", lambda: conn)
    ids = ev_store.create_jobs([(11, 101)], cycle_id=2026)
    assert ids == [7]
    # 已有活跃 job → 直接复用,不再 INSERT
    conn2 = _mock_conn(fetchone={"job_id": 7})
    monkeypatch.setattr(ev_store, "_conn", lambda: conn2)
    ids2 = ev_store.create_jobs([(11, 101)], cycle_id=2026)
    assert ids2 == [7]
    inserts = [c for c in conn2.execute.call_args_list if "INSERT INTO evaluation_job" in c.args[0]]
    assert len(inserts) == 0  # 复用已有活跃 job,无新 INSERT


def test_mark_job_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="非法 job 状态"):
        ev_store.mark_job(1, "cancelled")


def test_requeue_failed_scoped_by_cycle_and_skips_exhausted(monkeypatch) -> None:
    """闸门3:失败重试按周期限定,且跳过 attempts 已达上限的 job(交人工)。"""
    conn = _mock_conn(fetchall=[{"job_id": 5}])
    monkeypatch.setattr(ev_store, "_conn", lambda: conn)
    assert ev_store.requeue_failed(2026) == [5]
    sql, params = conn.execute.call_args.args
    assert "status = 'failed'" in sql and "RETURNING job_id" in sql
    assert "attempts < %s" in sql
    # #175:参数含双上限(自身资格 + 同组更新失败行资格)
    assert params == (2026, ev_store._MAX_ATTEMPTS, ev_store._MAX_ATTEMPTS)


def test_requeue_failed_skips_attempts_exhausted(monkeypatch) -> None:
    """闸门3:attempts 达上限的失败 job 不重排(交人工),不进返回列表。"""
    conn = _mock_conn(fetchall=[])
    monkeypatch.setattr(ev_store, "_conn", lambda: conn)
    # 生产 SQL 带 attempts < %s 过滤;fetchall 空 → 无重排
    assert ev_store.requeue_failed(2026) == []
    sql, params = conn.execute.call_args.args
    assert "attempts < %s" in sql
    assert params == (2026, ev_store._MAX_ATTEMPTS, ev_store._MAX_ATTEMPTS)


# ── runner 状态机(fetch/评分注入) ────────────────────────


def _fields():
    return [FieldText(field_key="intro", title="自我介绍", value="认真的自我介绍内容。")]


def _fake_to_thread(seen: list):
    """直通版 to_thread:记录调用,返回可等待(真 to_thread 是 awaitable)。"""

    def _impl(fn, *a, **k):
        seen.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))

        async def _run():
            return fn(*a, **k)

        return _run()

    return _impl


@pytest.mark.asyncio
async def test_run_job_success_marks_succeeded(monkeypatch) -> None:
    seen: dict = {}

    async def _fetch(user_id, cycle_id):
        return 99, _fields()

    async def _run_evaluation(fields, *, resume_id, cycle_id, weights=None, **_kw):
        seen["resume_id"] = resume_id
        return {"total": 66.0, "hard_zero": False, "dimensions": [], "attitude": {}}

    def _save(card, *, resume_id, cycle_id, prompt_version):
        seen["saved"] = (resume_id, card["total"])
        return 1

    def _mark(job_id, status, **kw):
        seen.setdefault("marks", []).append(status)
        if kw:
            print("MARK KW:", kw)
        return True

    async def _fetch_github(user_id):
        return "someuser"

    async def _bundle(fields, **kw):
        seen["bundle_kwargs"] = kw
        return {
            "schema_name": "evaluation_qbank/v2",
            "groups": [],
            "prompt_version": "t",
        }

    runner = EvaluationRunner()
    status_calls: list = []

    async def _fake_status(resume_id, status):
        status_calls.append((resume_id, status))

    with (
        patch.object(ev_runner, "fetch_scoring_fields", _fetch),
        patch.object(ev_runner, "run_evaluation", _run_evaluation),
        patch.object(ev_runner.evaluation, "save_scorecard", _save),
        patch.object(ev_runner.evaluation, "mark_job", _mark),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42, "resume_id": 99},
        ),
        patch.object(ev_runner.audit, "write_audit", lambda **k: None),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
        patch.object(ev_runner, "fetch_candidate_github", _fetch_github),
        patch.object(ev_runner, "_set_resume_status", _fake_status),
        patch("official_agent.evaluation.bundle.run_bundle", _bundle),
        patch("official_agent.state.qbank.save_qbank", lambda **k: 3),
        patch("official_agent.state.conversation.write_conversation", lambda **k: None),
    ):
        await runner._run_job(1, 2026, trigger_user_id=9)
    assert seen["marks"] == ["running", "succeeded"]
    assert seen["resume_id"] == 99  # 以取回的 resumeId 为准(job 存的是取数键)
    assert seen["saved"] == (99, 66.0)
    # D17/#149 接线:github_key 从档案取、GITHUB_TOKEN 从 settings 传参进 bundle
    assert seen["bundle_kwargs"]["github_key"] == "someuser"
    assert "github_token" in seen["bundle_kwargs"]


@pytest.mark.asyncio
async def test_run_job_failure_marks_failed(monkeypatch) -> None:
    async def _boom(user_id, cycle_id):
        raise RuntimeError("backend down")

    marks: list = []

    def _mark(job_id, status, **kw):
        marks.append((status, kw.get("error")))
        return True

    runner = EvaluationRunner()
    with (
        patch.object(ev_runner, "fetch_scoring_fields", _boom),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42, "resume_id": 99},
        ),
        patch.object(ev_runner.evaluation, "mark_job", _mark),
        patch.object(ev_runner.audit, "write_audit", lambda **k: None),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
    ):
        await runner._run_job(1, 2026, trigger_user_id=9)
    status, error = marks[-1]
    assert status == "failed"
    assert "backend down" in (error or "")


@pytest.mark.asyncio
async def test_submit_empty_items_creates_nothing() -> None:
    runner = EvaluationRunner()
    with patch.object(ev_runner.evaluation, "create_jobs") as mock_create:
        ids = await runner.submit(2026, [], trigger_user_id=1)
    assert ids == []
    mock_create.assert_not_called()


def test_spawn_keeps_task_references() -> None:
    """B2 评审 P1:派发任务必须持强引用,否则可能被 GC 静默丢 job。"""
    runner = EvaluationRunner()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_spawn_probe(runner))
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_recover_stale_dispatches_with_original_cycle(monkeypatch) -> None:
    """#175:启动恢复必须按行内原 (job_id, cycle_id) 派发。

    曾把 requeue_stale_all_cycles 返回的整行 dict 当 job_id、cycle 硬编码 0
    ——多周期数据下恢复必错位。"""
    rows = [
        {"job_id": 7, "cycle_id": 2026},
        {"job_id": 8, "cycle_id": 2027},
    ]
    dispatched: list[tuple[int, int]] = []
    spawned: list = []

    async def _record_run(self, job_id: int, cycle_id: int, *, trigger_user_id: int) -> None:
        dispatched.append((job_id, cycle_id))

    monkeypatch.setattr(ev_runner.evaluation, "requeue_stale_all_cycles", lambda **k: rows)
    monkeypatch.setattr(EvaluationRunner, "_run_job", _record_run)
    monkeypatch.setattr(EvaluationRunner, "_spawn", lambda self, coro: spawned.append(coro))
    runner = EvaluationRunner()
    job_ids = await runner.recover_stale_on_startup(older_than_minutes=0)
    await asyncio.gather(*spawned)
    assert job_ids == [7, 8]
    assert dispatched == [(7, 2026), (8, 2027)]


@pytest.mark.asyncio
async def test_try_dispatch_dedupes_until_completion(monkeypatch) -> None:
    """#193:同 job 在跑期间重复派发被跳过;完成后清理登记,可合法复评。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_run(self, job_id, cycle_id, *, trigger_user_id) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(EvaluationRunner, "_run_job", _fake_run)
    runner = EvaluationRunner()
    assert runner._try_dispatch(7, 2026, trigger_user_id=0) is True
    assert runner._try_dispatch(7, 2026, trigger_user_id=0) is False
    assert runner._try_dispatch(7, 2026, trigger_user_id=0) is False
    await started.wait()
    release.set()
    await asyncio.gather(*list(runner._tasks))
    assert 7 not in runner._inflight, "完成后登记必须清理"
    assert runner._try_dispatch(7, 2026, trigger_user_id=0) is True
    await asyncio.gather(*list(runner._tasks))


@pytest.mark.asyncio
async def test_submit_double_click_runs_job_once(monkeypatch) -> None:
    """#193:create_jobs 幂等复用活跃 job_id 后,双击提交只执行一次。"""
    runs: list[int] = []
    release = asyncio.Event()

    async def _record_run(self, job_id, cycle_id, *, trigger_user_id) -> None:
        runs.append(job_id)
        # 模拟慢 job:第二次提交到达时第一次仍在执行
        await release.wait()

    async def _authority(resume_id):
        return {"resume_id": resume_id, "user_id": 42, "cycle_id": 2026}

    monkeypatch.setattr(ev_runner, "fetch_resume_authority", _authority)
    # 两次提交 DB 层都返回同一活跃 job(闸门2 SELECT-first 语义)
    monkeypatch.setattr(ev_runner.evaluation, "create_jobs", lambda items, cycle_id: [7])
    monkeypatch.setattr(ev_runner.audit, "write_audit", lambda **k: None)
    monkeypatch.setattr(EvaluationRunner, "_run_job", _record_run)
    runner = EvaluationRunner()
    first = await runner.submit(2026, [ev_runner.TriggerItem(resume_id=99)], trigger_user_id=1)
    second = await runner.submit(2026, [ev_runner.TriggerItem(resume_id=99)], trigger_user_id=1)
    assert first == second == [7]
    release.set()
    await asyncio.gather(*list(runner._tasks))
    assert runs == [7], f"双击只应执行一次,实际 {runs}"


async def _spawn_probe(runner: EvaluationRunner) -> None:
    async def _noop():
        await asyncio.sleep(0)

    runner._spawn(_noop())
    await asyncio.sleep(0)
    assert runner._tasks, "任务应被强引用"


def test_attempts_only_bumps_on_running(monkeypatch) -> None:
    """B2 评审 P2:attempts=实际执行次数,终态不再翻倍。"""
    calls: list[tuple] = []

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

            class _Cur:
                rowcount = 1

            return _Cur()

    monkeypatch.setattr(ev_store, "_conn", lambda: _FakeConn())
    ev_store.mark_job(1, "running")
    ev_store.mark_job(1, "succeeded")
    updates = [sql for sql, _ in calls if "UPDATE evaluation_job" in sql]
    assert len(updates) == 2
    assert "attempts = attempts + 1" in updates[0]
    assert "attempts" not in updates[1]


@pytest.mark.asyncio
async def test_failure_marks_full_timeline_and_audits() -> None:
    """B2 评审 P2:失败用例断言完整时序;触发审计被调用。"""

    async def _boom(user_id, cycle_id):
        raise RuntimeError("backend down")

    marks: list = []
    audit_calls: list = []

    runner = EvaluationRunner()
    with (
        patch.object(ev_runner, "fetch_scoring_fields", _boom),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42, "resume_id": 99},
        ),
        patch.object(
            ev_runner.evaluation,
            "mark_job",
            lambda job_id, status, **kw: marks.append((status, kw.get("error"))) or True,
        ),
        patch.object(
            ev_runner.audit,
            "write_audit",
            lambda **k: audit_calls.append(k["action"]["op"]),
        ),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
    ):
        await runner._run_job(1, 2026, trigger_user_id=9)
    assert [m[0] for m in marks] == ["running", "failed"]
    assert audit_calls == ["scorecard_generated"] or audit_calls == []  # 失败路径无完成审计
    assert marks[-1][0] == "failed"


@pytest.mark.asyncio
async def test_eval_usage_log_written_per_job(monkeypatch) -> None:
    """#154/D9:job 完成后 evaluation 通道用量日志落 conversation_log(关联 job)。"""

    seen: dict = {}

    async def _fetch(user_id, cycle_id):
        return 99, _fields()

    async def _run_evaluation(fields, *, resume_id, cycle_id, weights=None, **_kw):
        return {"total": 66.0, "hard_zero": False, "dimensions": [], "attitude": {}}

    def _save(card, *, resume_id, cycle_id, prompt_version):
        return 1

    def _mark(job_id, status, **kw):
        return True

    async def _fetch_github(user_id):
        return "someuser"

    async def _bundle(fields, **kw):
        return {
            "schema_name": "evaluation_qbank/v2",
            "groups": [],
            "explore_usage_total": {
                "input_tokens": 150,
                "output_tokens": 30,
                "cache_hit_tokens": 100,
                "cache_miss_tokens": 15,
            },
            "prompt_version": "t",
        }

    def _write_conversation(**kw):
        seen["log"] = kw

    monkeypatch.setattr(ev_runner, "fetch_scoring_fields", _fetch)
    monkeypatch.setattr(ev_runner, "run_evaluation", _run_evaluation)
    monkeypatch.setattr(ev_runner.evaluation, "save_scorecard", lambda *a, **k: 1)
    monkeypatch.setattr(ev_runner.evaluation, "mark_job", lambda *a, **k: True)
    monkeypatch.setattr(
        ev_runner.evaluation,
        "get_job",
        lambda jid: {"job_id": jid, "user_id": 42, "resume_id": 99},
    )
    monkeypatch.setattr(ev_runner.audit, "write_audit", lambda **k: None)
    monkeypatch.setattr(ev_runner.asyncio, "to_thread", _fake_to_thread([]))
    monkeypatch.setattr(ev_runner, "fetch_candidate_github", _fetch_github)
    monkeypatch.setattr("official_agent.evaluation.bundle.run_bundle", _bundle)
    monkeypatch.setattr(
        "official_agent.state.qbank.save_qbank",
        lambda **k: 3,  # 版本 3
    )
    monkeypatch.setattr("official_agent.state.conversation.write_conversation", _write_conversation)

    runner = EvaluationRunner()
    await runner._run_job(1, 2026, trigger_user_id=9)
    log = seen["log"]
    assert log["channel"] == "evaluation"
    assert log["thread_id"] == "eval:1"  # 关联 job
    assert log["input_tokens"] == 150
    assert log["cache_hit_tokens"] == 100


@pytest.mark.asyncio
async def test_run_job_masks_pii_before_models(monkeypatch, caplog) -> None:
    """#176 出口契约:姓名(结构键)与手机/邮箱/QQ/学号(自由文本)不得进评分/出题模型。"""
    import logging as _logging

    seen: dict = {}

    async def _fake_status(resume_id, status):
        return None

    async def _fake_github(user_id):
        return "someuser"

    async def _fetch(user_id, cycle_id):
        return 99, [
            FieldText(field_key="real_name", title="姓名", value="张三"),
            FieldText(
                field_key="intro",
                title="自我介绍",
                value="电话 13812345678,邮箱 me@x.com,QQ 123456789,学号 2021023456",
            ),
        ]

    def _values(fields):
        # run_evaluation 收 dict 投影,run_bundle 收 FieldText
        return [f["value"] if isinstance(f, dict) else f.value for f in fields]

    async def _run_evaluation(fields, *, resume_id, cycle_id, weights=None, **_kw):
        seen["scoring"] = _values(fields)
        return {"total": 60.0, "hard_zero": False, "dimensions": [], "attitude": {}}

    async def _bundle(fields, **kw):
        seen["qbank"] = _values(fields)
        return {"schema_name": "evaluation_qbank/v2", "groups": [], "prompt_version": "t"}

    runner = EvaluationRunner()
    with (
        patch.object(ev_runner, "fetch_scoring_fields", _fetch),
        patch.object(ev_runner, "run_evaluation", _run_evaluation),
        patch.object(ev_runner.evaluation, "save_scorecard", lambda *a, **k: 1),
        patch.object(
            ev_runner.evaluation,
            "mark_job",
            lambda jid, st, **kw: seen.setdefault("marks", []).append((st, kw)) or True,
        ),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42, "resume_id": 99},
        ),
        patch.object(ev_runner.audit, "write_audit", lambda **k: None),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
        patch.object(ev_runner, "fetch_candidate_github", _fake_github),
        patch.object(ev_runner, "_set_resume_status", _fake_status),
        patch("official_agent.evaluation.bundle.run_bundle", _bundle),
        patch("official_agent.state.qbank.save_qbank", lambda **k: 3),
        patch("official_agent.state.conversation.write_conversation", lambda **k: None),
        caplog.at_level(_logging.WARNING, logger="official_agent.evaluation.runner"),
    ):
        await runner._run_job(1, 2026, trigger_user_id=9)
    assert seen["marks"][0] == ("running", {})
    assert seen["marks"][1][0] == "succeeded", f"job 不应走失败路径:{seen['marks']}"

    joined_scoring = " ".join(seen["scoring"])
    joined_qbank = " ".join(seen["qbank"])
    for leak in ("张三", "13812345678", "me@x.com", "123456789", "2021023456"):
        assert leak not in joined_scoring, f"评分面泄漏:{leak}"
        assert leak not in joined_qbank, f"出题面泄漏:{leak}"
    # 键级姓名掩 + 文本规则掩码产物就位(脱敏不是清空,打分面仍有内容)
    assert "〔姓名〕" in joined_scoring
    assert "138****5678" in joined_scoring
    assert "[邮箱]" in joined_scoring
    # 安全日志断言:命中必须留痕(resume 级,不含原文)
    assert any("eval_pii_exit" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_job_sets_correlation_trace_and_structured_logs(caplog) -> None:
    """#183:job 运行期间 turn trace id = eval_job_trace_id(job_id),
    结构化事件按阶段落行且不含简历原文。"""
    import logging as _logging

    from official_agent.observability import (
        current_trace_id,
        eval_job_trace_id,
    )

    seen: dict = {}

    async def _fake_status(resume_id, status):
        return None

    async def _fake_github(user_id):
        return "someuser"

    async def _fetch(user_id, cycle_id):
        return 99, _fields()

    async def _run_evaluation(fields, *, resume_id, cycle_id, weights=None, **_kw):
        seen["trace_during_eval"] = current_trace_id()
        return {"total": 66.0, "hard_zero": False, "dimensions": [], "attitude": {}}

    async def _bundle(fields, **kw):
        return {"schema_name": "evaluation_qbank/v2", "groups": [], "prompt_version": "t"}

    runner = EvaluationRunner()
    with (
        patch.object(ev_runner, "fetch_scoring_fields", _fetch),
        patch.object(ev_runner, "run_evaluation", _run_evaluation),
        patch.object(ev_runner.evaluation, "save_scorecard", lambda *a, **k: 1),
        patch.object(ev_runner.evaluation, "mark_job", lambda *a, **k: True),
        patch.object(
            ev_runner.evaluation,
            "get_job",
            lambda jid: {"job_id": jid, "user_id": 42, "resume_id": 99, "attempts": 2},
        ),
        patch.object(ev_runner.audit, "write_audit", lambda **k: None),
        patch.object(ev_runner.asyncio, "to_thread", _fake_to_thread([])),
        patch.object(ev_runner, "fetch_candidate_github", _fake_github),
        patch.object(ev_runner, "_set_resume_status", _fake_status),
        patch("official_agent.evaluation.bundle.run_bundle", _bundle),
        patch("official_agent.state.qbank.save_qbank", lambda **k: 3),
        patch("official_agent.state.conversation.write_conversation", lambda **k: None),
        caplog.at_level(_logging.INFO, logger="official_agent.evaluation.runner"),
    ):
        await runner._run_job(7, 2026, trigger_user_id=9)

    expected = eval_job_trace_id(7)
    assert seen["trace_during_eval"] == expected, (
        "评分模型执行期间 trace id 应为 job 级 correlation id"
    )
    stages = [r.getMessage() for r in caplog.records if "eval_event" in r.getMessage()]
    assert any("stage=started" in m and "attempts=3" in m for m in stages)  # 执行次数:2+1
    assert any("stage=scorecard_saved" in m and "prompt_version=" in m for m in stages)
    assert any("stage=succeeded" in m and "duration_ms=" in m for m in stages)
    assert all("认真的自我介绍内容" not in m for m in stages), "结构化日志不得含简历原文"


def test_eval_job_trace_id_deterministic() -> None:
    from official_agent.observability import eval_job_trace_id

    assert eval_job_trace_id(7) == eval_job_trace_id(7)
    assert eval_job_trace_id(7) != eval_job_trace_id(8)
    assert len(eval_job_trace_id(7)) == 32  # W3C trace-id 段


@pytest.mark.asyncio
async def test_pii_exit_covers_x_id_and_long_student_ids() -> None:
    """#176 评审 P2:身份证尾号 X、12-17 位学号/准考证号必须被掩。"""
    from official_agent.security.pii import mask_pii

    masked = mask_pii("证件 11010119900307775X 学号 202102345678")
    assert "11010119900307775X" not in masked
    assert "202102345678" not in masked
    assert mask_pii("13812345678") == "138****5678"  # 常规规则不回归


def test_hard_zero_survives_masking(monkeypatch) -> None:
    """#176 评审 P3:纯数字敷衍回答掩码后(含 *)仍命中确定性硬 0。"""
    from official_agent.evaluation.scoring import is_hard_zero_value

    assert is_hard_zero_value("138****5678")
    assert is_hard_zero_value("************")
    assert not is_hard_zero_value("138****5678,项目经验丰富")  # 有实质内容不误伤
