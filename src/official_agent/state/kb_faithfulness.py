"""回复忠实度抽样的状态面:运行 kb_faithfulness_run 与样本 kb_faithfulness_sample。

一次抽样是一行运行,它抽到的每一轮对话是一行样本(随运行级联删除)。样本里的逐条
说法出自对话回复,属于会话留存:样本用外键挂在对话日志行上并级联删除——用户删
会话、会话过期清理时,对话日志一删,样本跟着没了,不需要在删除路径上另接一步。
只保留最近若干次运行。

抽样的候选是「这一轮检索过知识库且有命中、回复已正常发出」的对话日志行,靠
trace_id 与检索日志对上。同一时刻只允许一次抽样运行(advisory lock)。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from official_agent.config import get_settings
from official_agent.kb.query_log import ensure_query_log_table
from official_agent.state.conversation import ensure_conversation_table
from official_agent.state.pools import pooled_conn

#: 样本的核对结局。judged 之外的三种没有逐条结论:
#: failed 模型调用或输出校验失败;no_evidence 当时命中的条目现在都读不到了(被删、
#: 停用或换届);out_of_budget 运行的墙钟预算用完
SAMPLE_JUDGED = "judged"
SAMPLE_FAILED = "failed"
SAMPLE_NO_EVIDENCE = "no_evidence"
SAMPLE_OUT_OF_BUDGET = "out_of_budget"

_RUN_LOCK_NAME = "official_agent.kb_faithfulness.run"
_INTERRUPTED_ERROR = "进程中断,运行没有完成"
_RUN_COLUMNS = (
    "run_id, status, window_start, window_end, triggered_by, params, stats, "
    "prompt_versions, error, started_at, finished_at"
)
#: 「可以抽样的一轮」:回复正常发出,且这一轮有命中的知识库检索
_CANDIDATE_WHERE = """
    c.created_at >= %s AND c.created_at < %s
    AND c.error_code IS NULL AND c.reply_summary <> '' AND c.trace_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM kb_query_log q
        WHERE q.trace_id = c.trace_id AND q.thread_id = c.thread_id AND q.status = 'ok'
    )
"""


@dataclass(frozen=True)
class SampleRow:
    """待落库的一个样本。claims 是逐条结论 [{text, verdict, reason, chunk_id, quote}]。"""

    conversation_id: int
    status: str
    source_ids: tuple[str, ...] = ()
    claims_total: int = 0
    supported: int = 0
    unsupported: int = 0
    contradicted: int = 0
    claims: tuple[dict[str, Any], ...] = ()


_ready_urls: set[str] = set()
_ready_lock = threading.Lock()


def reset_table_cache() -> None:
    """清空「表已建好」的记忆(测试改库/删表后用;生产不调用)。"""
    with _ready_lock:
        _ready_urls.clear()


def ensure_faithfulness_tables() -> None:
    """幂等建两张表;每个库每进程只真正执行一次。

    样本表的外键指向对话日志,抽样还要读检索日志,所以先确保那两张表存在。
    """
    url = get_settings().postgres_url
    if url in _ready_urls:
        return
    with _ready_lock:
        if url in _ready_urls:
            return
        ensure_conversation_table()
        ensure_query_log_table()
        with pooled_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_faithfulness_run (
                    run_id          text        PRIMARY KEY,
                    status          text        NOT NULL
                                    CHECK (status IN ('running', 'succeeded', 'failed')),
                    window_start    timestamptz NOT NULL,
                    window_end      timestamptz NOT NULL,
                    triggered_by    integer     NOT NULL,
                    params          jsonb       NOT NULL DEFAULT '{}',
                    stats           jsonb       NOT NULL DEFAULT '{}',
                    prompt_versions jsonb       NOT NULL DEFAULT '{}',
                    error           text,
                    started_at      timestamptz NOT NULL DEFAULT now(),
                    finished_at     timestamptz
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_faithfulness_sample (
                    sample_id       bigserial   PRIMARY KEY,
                    run_id          text        NOT NULL
                                    REFERENCES kb_faithfulness_run(run_id) ON DELETE CASCADE,
                    conversation_id bigint      NOT NULL
                                    REFERENCES agent_conversation_log(id) ON DELETE CASCADE,
                    status          text        NOT NULL
                                    CHECK (status IN (
                                        'judged', 'failed', 'no_evidence', 'out_of_budget')),
                    source_ids      text[]      NOT NULL DEFAULT '{}',
                    claims_total    integer     NOT NULL DEFAULT 0,
                    supported       integer     NOT NULL DEFAULT 0,
                    unsupported     integer     NOT NULL DEFAULT 0,
                    contradicted    integer     NOT NULL DEFAULT 0,
                    claims          jsonb       NOT NULL DEFAULT '[]',
                    UNIQUE (run_id, conversation_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_faithfulness_run_started "
                "ON kb_faithfulness_run (started_at DESC)"
            )
            # 对话日志行被删时要级联到样本:没有这个索引,每删一行都要扫整张样本表
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_faithfulness_sample_conversation "
                "ON kb_faithfulness_sample (conversation_id)"
            )
        _ready_urls.add(url)


def try_acquire_run_lock() -> psycopg.Connection[Any] | None:
    """抢「正在抽样」的锁。抢到返回持锁连接(用完 close 即释放),已有运行在跑返回 None。"""
    conn = psycopg.connect(get_settings().postgres_url, autocommit=True)
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (_RUN_LOCK_NAME,)
        ).fetchone()
    except Exception:
        conn.close()
        raise
    if row and row[0]:
        return conn
    conn.close()
    return None


def count_candidates(*, since: datetime, until: datetime) -> int:
    """时间窗 [since, until) 内可以抽样的对话轮数。"""
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT count(*) AS n FROM agent_conversation_log c WHERE {_CANDIDATE_WHERE}",
            (since, until),
        ).fetchone()
    return int(row["n"]) if row else 0


def sample_candidates(
    *, since: datetime, until: datetime, seed: str, limit: int
) -> list[dict[str, Any]]:
    """从可以抽样的对话轮里抽 limit 轮,带上各自命中的条目与当时的周期过滤值。

    抽样顺序由 seed 决定(同一个 seed 抽到同一批),与对话的先后无关。每项含
    conversation_id / thread_id / user_message / reply_summary / created_at /
    source_ids(命中的条目,保持命中顺序、去重)/ cycle_ids。
    """
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        turns = conn.execute(
            f"""
            SELECT c.id AS conversation_id, c.thread_id, c.trace_id, c.user_message,
                   c.reply_summary, c.created_at
            FROM agent_conversation_log c
            WHERE {_CANDIDATE_WHERE}
            ORDER BY md5(c.id::text || %s), c.id
            LIMIT %s
            """,
            (since, until, seed, limit),
        ).fetchall()
        if not turns:
            return []
        searches = conn.execute(
            """
            SELECT trace_id, thread_id, hit_source_ids, cycle_ids
            FROM kb_query_log
            WHERE status = 'ok' AND trace_id = ANY(%s::text[])
            ORDER BY id
            """,
            ([t["trace_id"] for t in turns],),
        ).fetchall()
    by_turn: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for search in searches:
        entry = by_turn.setdefault(
            (search["trace_id"], search["thread_id"]), {"source_ids": [], "cycle_ids": []}
        )
        entry["source_ids"].extend(search["hit_source_ids"] or [])
        entry["cycle_ids"].extend(search["cycle_ids"] or [])
    samples = []
    for turn in turns:
        entry = by_turn.get((turn["trace_id"], turn["thread_id"]), {})
        samples.append(
            {
                "conversation_id": int(turn["conversation_id"]),
                "thread_id": turn["thread_id"],
                "user_message": turn["user_message"],
                "reply_summary": turn["reply_summary"],
                "created_at": turn["created_at"],
                "source_ids": list(dict.fromkeys(entry.get("source_ids", []))),
                "cycle_ids": sorted(set(entry.get("cycle_ids", []))),
            }
        )
    return samples


def summarize_citation_verdicts(*, since: datetime, until: datetime) -> dict[str, int]:
    """时间窗内在线引用核验的判定计数:clean / unverified / unsearched。

    数的是对话日志 guard_verdict 里 kb_citation 那一项;引用核验没打开的时期全是 0。
    """
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        row = conn.execute(
            """
            SELECT count(*) FILTER (WHERE guard_verdict LIKE %s) AS clean,
                   count(*) FILTER (WHERE guard_verdict LIKE %s) AS unverified,
                   count(*) FILTER (WHERE guard_verdict LIKE %s) AS unsearched
            FROM agent_conversation_log
            WHERE created_at >= %s AND created_at < %s AND guard_verdict IS NOT NULL
            """,
            (
                "%kb_citation=clean%",
                "%kb_citation=unverified:%",
                "%kb_citation=unsearched:%",
                since,
                until,
            ),
        ).fetchone()
    return {key: int(row[key]) if row else 0 for key in ("clean", "unverified", "unsearched")}


def create_run(
    run_id: str,
    *,
    window_start: datetime,
    window_end: datetime,
    triggered_by: int,
    params: dict[str, Any],
) -> None:
    """登记一次新运行(status=running)。必须在持有运行锁时调用。

    同一事务里把遗留的 running 行改记为 failed:持锁说明没有别的运行在跑,那些行
    只能是进程中断留下的。
    """
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        conn.execute(
            "UPDATE kb_faithfulness_run SET status = 'failed', error = %s, finished_at = now() "
            "WHERE status = 'running'",
            (_INTERRUPTED_ERROR,),
        )
        conn.execute(
            """
            INSERT INTO kb_faithfulness_run
                (run_id, status, window_start, window_end, triggered_by, params)
            VALUES (%s, 'running', %s, %s, %s, %s)
            """,
            (run_id, window_start, window_end, triggered_by, Jsonb(params)),
        )


def finish_run(
    run_id: str,
    *,
    stats: dict[str, Any],
    prompt_versions: dict[str, str],
    samples: Sequence[SampleRow],
) -> None:
    """运行成功:样本与运行状态在一个事务里落库。

    抽样之后、落库之前,样本所在的会话可能已被用户删除:对话日志行不在了的样本
    直接跳过,不让外键错误拖垮整次运行。
    """
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        for s in samples:
            conn.execute(
                """
                INSERT INTO kb_faithfulness_sample
                    (run_id, conversation_id, status, source_ids, claims_total, supported,
                     unsupported, contradicted, claims)
                SELECT %s, c.id, %s, %s::text[], %s, %s, %s, %s, %s
                FROM agent_conversation_log c WHERE c.id = %s
                """,
                (
                    run_id,
                    s.status,
                    list(s.source_ids),
                    s.claims_total,
                    s.supported,
                    s.unsupported,
                    s.contradicted,
                    Jsonb(list(s.claims)),
                    s.conversation_id,
                ),
            )
        conn.execute(
            """
            UPDATE kb_faithfulness_run
            SET status = 'succeeded', stats = %s, prompt_versions = %s, finished_at = now()
            WHERE run_id = %s
            """,
            (Jsonb(stats), Jsonb(prompt_versions), run_id),
        )


def fail_run(run_id: str, error: str) -> None:
    """运行失败:记下面向管理员的失败原因。error 由调用方保证不含内部细节。"""
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        conn.execute(
            "UPDATE kb_faithfulness_run SET status = 'failed', error = %s, finished_at = now() "
            "WHERE run_id = %s",
            (error, run_id),
        )


def purge_old_runs(keep: int) -> int:
    """只留最近 keep 次运行,更早的连同样本删除。返回删掉的运行数。"""
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        cur = conn.execute(
            """
            DELETE FROM kb_faithfulness_run
            WHERE run_id NOT IN (
                SELECT run_id FROM kb_faithfulness_run
                ORDER BY started_at DESC, run_id DESC LIMIT %s
            )
            """,
            (keep,),
        )
        return cur.rowcount


def list_runs(*, limit: int = 20) -> list[dict[str, Any]]:
    """最近的运行,新的在前。"""
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        rows = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM kb_faithfulness_run "
            "ORDER BY started_at DESC, run_id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM kb_faithfulness_run WHERE run_id = %s", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def latest_succeeded_run() -> dict[str, Any] | None:
    """最近一次成功的运行;一次都没成功过返回 None。"""
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM kb_faithfulness_run WHERE status = 'succeeded' "
            "ORDER BY started_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def list_samples(run_id: str) -> list[dict[str, Any]]:
    """一次运行的样本,带上那一轮的问题与回复(从对话日志现读),问题多的排前面。"""
    ensure_faithfulness_tables()
    with pooled_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.sample_id, s.run_id, s.conversation_id, s.status, s.source_ids,
                   s.claims_total, s.supported, s.unsupported, s.contradicted, s.claims,
                   c.user_message, c.reply_summary, c.created_at AS asked_at
            FROM kb_faithfulness_sample s
            JOIN agent_conversation_log c ON c.id = s.conversation_id
            WHERE s.run_id = %s
            ORDER BY s.contradicted DESC, s.unsupported DESC, s.sample_id
            """,
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]
