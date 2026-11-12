"""评估用量日志真库集成测试:评分段与题库段各落一行,库里能按段分开统计。

需要真 PostgreSQL(POSTGRES_URL 指向可写库);无 PG 时整文件 skip。
替身连接验不出两件事,而按段统计成本恰恰依赖它们:tools 列(jsonb)上的
包含查询能把两段分开;没有用量时 token 列是 NULL 而不是 0。
"""

import uuid
from collections.abc import Iterator

import pytest

psycopg = pytest.importorskip("psycopg")

from psycopg.rows import dict_row  # noqa: E402
from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.evaluation import runner as ev_runner  # noqa: E402
from official_agent.evaluation.applicant import ApplicantProfile  # noqa: E402
from official_agent.evaluation.scoring import FieldText  # noqa: E402
from official_agent.state import conversation as conversation_store  # noqa: E402
from official_agent.state import evaluation as ev_store  # noqa: E402
from official_agent.state import qbank as qbank_store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")

_SCORING_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 340,
    "cache_hit_tokens": 800,
    "cache_miss_tokens": 400,
}
_QBANK_USAGE = {
    "input_tokens": 150,
    "output_tokens": 30,
    "cache_hit_tokens": 100,
    "cache_miss_tokens": 15,
}

#: 按段汇总一个周期的评估用量。用量行靠 thread_id(eval:<job_id>)关联回 job 才知道
#: 属于哪个周期;段标记直接写字面量——下游统计也只能这样认,这里钉的就是这份约定。
_STAGE_TOTALS_SQL = """
    SELECT c.tools @> '["scoring"]'::jsonb AS is_scoring,
           COUNT(*) AS row_count,
           SUM(c.input_tokens) AS input_tokens,
           SUM(c.output_tokens) AS output_tokens
    FROM agent_conversation_log c
    JOIN evaluation_job j
      ON j.job_id = substring(c.thread_id from '^eval:([0-9]{1,18})$')::bigint
    WHERE c.channel = 'evaluation' AND j.cycle_id = %s
    GROUP BY 1
"""


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_tables():
    """建表一次,先于所有用例:数据路径上不做 DDL,干净库得先自举。"""
    previous = point_settings_at_pg()
    ev_store.reset_pool()
    try:
        conversation_store.ensure_conversation_table()
        ev_store.ensure_evaluation_scorecard_ready()
        ev_store.ensure_evaluation_job_ready()
        qbank_store.ensure_qbank_ready()
        yield
    finally:
        point_settings_at_pg(previous)


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL)
    # 池按 postgres_url 懒建;patch 后重建,保证池连接指向本测试的库
    ev_store.reset_pool()


@pytest.fixture
def job() -> Iterator[dict]:
    """一个真实落库的 job(独立 cycle_id,不与别的用例或残留数据相扰);用完清掉它名下的行。"""
    cycle_id = uuid.uuid4().int % 2_000_000_000 + 1
    resume_id, user_id = 9001, 501
    job_id = ev_store.create_jobs([(resume_id, user_id)], cycle_id)[0]
    try:
        yield {"job_id": job_id, "user_id": user_id, "resume_id": resume_id, "cycle_id": cycle_id}
    finally:
        with psycopg.connect(PG_URL) as conn:
            conn.execute(
                "DELETE FROM agent_conversation_log WHERE thread_id = %s", (f"eval:{job_id}",)
            )
            conn.execute("DELETE FROM interview_qbank WHERE cycle_id = %s", (cycle_id,))
            conn.execute("DELETE FROM evaluation_scorecard WHERE cycle_id = %s", (cycle_id,))
            conn.execute("DELETE FROM evaluation_job WHERE cycle_id = %s", (cycle_id,))


async def _run_both_stages(
    monkeypatch: pytest.MonkeyPatch, job: dict, *, scoring_usage: dict, qbank_usage: dict
) -> None:
    """对真库跑评分段与题库段;只把后端取数和两个模型入口换成替身。"""

    async def _fetch(user_id, cycle_id):
        fields = [FieldText(field_key="intro", title="自我介绍", value="认真的自我介绍内容。")]
        return job["resume_id"], fields, ApplicantProfile()

    async def _run_evaluation(fields, *, usage_out=None, **_kw):
        usage_out.update(scoring_usage)
        return {"total": 66.0, "hard_zero": False, "dimensions": [], "attitude": {}}

    async def _bundle(fields, **_kw):
        return {
            "schema_name": "evaluation_qbank/v2",
            "groups": [],
            "explore_usage_total": qbank_usage,
            "prompt_version": "t",
        }

    async def _no_github(user_id):
        return ""

    monkeypatch.setattr(ev_runner, "fetch_scoring_fields", _fetch)
    monkeypatch.setattr(ev_runner, "run_evaluation", _run_evaluation)
    monkeypatch.setattr(ev_runner, "fetch_candidate_github", _no_github)
    monkeypatch.setattr("official_agent.evaluation.bundle.run_bundle", _bundle)

    _card, _version, fields, grade = await ev_runner._do_scoring(
        job, job["resume_id"], job["cycle_id"], job["job_id"]
    )
    qbank_status, qbank_error, _ = await ev_runner._do_qbank(
        job,
        fields,
        grade,
        resume_id=job["resume_id"],
        cycle_id=job["cycle_id"],
        job_id=job["job_id"],
    )
    assert (qbank_status, qbank_error) == ("succeeded", None)


def _stage_totals(cycle_id: int) -> dict[bool, dict]:
    with psycopg.connect(PG_URL, row_factory=dict_row) as conn:
        rows = conn.execute(_STAGE_TOTALS_SQL, (cycle_id,)).fetchall()
    return {r["is_scoring"]: r for r in rows}


async def test_stage_usage_rows_are_separable_in_sql(monkeypatch, job: dict) -> None:
    """一个 job 的两行用量,按 tools 列的段标记能分开汇总,token 互不串段。"""
    await _run_both_stages(
        monkeypatch, job, scoring_usage=_SCORING_USAGE, qbank_usage=_QBANK_USAGE
    )

    totals = _stage_totals(job["cycle_id"])

    assert set(totals) == {True, False}
    scoring, qbank = totals[True], totals[False]
    assert (scoring["row_count"], scoring["input_tokens"], scoring["output_tokens"]) == (
        1,
        _SCORING_USAGE["input_tokens"],
        _SCORING_USAGE["output_tokens"],
    )
    assert (qbank["row_count"], qbank["input_tokens"], qbank["output_tokens"]) == (
        1,
        _QBANK_USAGE["input_tokens"],
        _QBANK_USAGE["output_tokens"],
    )


async def test_scoring_row_fields_round_trip(monkeypatch, job: dict) -> None:
    """评分段那一行在库里的样子:通道、归属人、段标记、四列 token、对应的卡版本。"""
    await _run_both_stages(
        monkeypatch, job, scoring_usage=_SCORING_USAGE, qbank_usage=_QBANK_USAGE
    )

    with psycopg.connect(PG_URL, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT channel, user_id, tools, reply_summary,
                   input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens
            FROM agent_conversation_log
            WHERE thread_id = %s AND tools @> '["scoring"]'::jsonb
            """,
            (f"eval:{job['job_id']}",),
        ).fetchone()

    assert row is not None
    assert row["channel"] == "evaluation"
    assert row["user_id"] == job["user_id"]
    assert row["tools"] == ["scoring"]
    assert {k: row[k] for k in _SCORING_USAGE} == _SCORING_USAGE
    assert row["reply_summary"] == "评分卡 v1 落库"  # 该简历在这个周期的第一版卡


async def test_missing_scoring_usage_is_null_not_zero(monkeypatch, job: dict) -> None:
    """评分子图没回传用量 → 行照落,token 四列是 NULL:「没有记录」不能读成「花了 0」。"""
    await _run_both_stages(monkeypatch, job, scoring_usage={}, qbank_usage=_QBANK_USAGE)

    with psycopg.connect(PG_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT input_tokens, output_tokens, cache_hit_tokens, cache_miss_tokens
            FROM agent_conversation_log
            WHERE thread_id = %s AND tools @> '["scoring"]'::jsonb
            """,
            (f"eval:{job['job_id']}",),
        ).fetchall()

    assert rows == [
        {
            "input_tokens": None,
            "output_tokens": None,
            "cache_hit_tokens": None,
            "cache_miss_tokens": None,
        }
    ]
    # 汇总时这一段的和是 NULL(没有可加的数),题库段不受影响
    totals = _stage_totals(job["cycle_id"])
    assert totals[True]["input_tokens"] is None
    assert totals[False]["input_tokens"] == _QBANK_USAGE["input_tokens"]
