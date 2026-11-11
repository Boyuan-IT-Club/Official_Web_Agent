"""知识库运营的运行记录与主题清单:kb_ops_run / kb_ops_topic 两张表。

一次离线挖掘是一行 kb_ops_run,它产出的主题是若干行 kb_ops_topic(随运行级联删除)。

约束:
- 主题行不存用户问题的文本,只存检索日志行的 id(sample_query_ids),展示时回查
  kb_query_log:用户删了会话,那几条样本也就查不到了。模型写的主题名、标准问法与
  理由是对多人提问的概括,随运行保留
- 只保留最近若干次运行(purge_old_runs),更早的连同主题一起删
- 同一时刻只允许一次运行:会话级 advisory lock,持锁连接一断开锁就释放,进程崩溃
  不会留下死锁;崩溃留下的 running 行在下一次运行开始时改记为 failed
- 表在首次使用时幂等建出,每个库每进程只执行一次
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
from official_agent.state.pools import pooled_conn

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"

#: 甄别结局。done 之外的四种都没有 scope:
#: failed 模型调用或输出校验失败;unsafe_samples 样本全部像指令,没有可以给模型看的;
#: out_of_budget 运行的墙钟预算用完;not_selected 排名在本次甄别名额之外
TRIAGE_DONE = "done"
TRIAGE_FAILED = "failed"
TRIAGE_UNSAFE_SAMPLES = "unsafe_samples"
TRIAGE_OUT_OF_BUDGET = "out_of_budget"
TRIAGE_NOT_SELECTED = "not_selected"

#: advisory lock 的名字;库内唯一即可,取哈希后作为锁键
_RUN_LOCK_NAME = "official_agent.kb_ops.run"
_INTERRUPTED_ERROR = "进程中断,运行没有完成"

_RUN_COLUMNS = (
    "run_id, status, window_start, window_end, triggered_by, params, stats, "
    "prompt_versions, error, started_at, finished_at"
)
_TOPIC_COLUMNS = (
    "topic_id, run_id, rank, query_count, thread_count, near_miss_count, best_vec_score, "
    "first_seen, last_seen, sample_query_ids, triage_status, scope, topic, "
    "canonical_question, reason"
)


@dataclass(frozen=True)
class TopicRow:
    """待落库的一个主题。scope / topic / canonical_question / reason 只在甄别完成时有值。"""

    rank: int
    query_count: int
    thread_count: int
    near_miss_count: int
    best_vec_score: float | None
    first_seen: datetime
    last_seen: datetime
    sample_query_ids: tuple[int, ...]
    triage_status: str
    scope: str | None = None
    topic: str | None = None
    canonical_question: str | None = None
    reason: str | None = None


#: 已建好表的库 URL。按 URL 记:集成测试会把 URL 指到临时库。
_ready_urls: set[str] = set()
_ready_lock = threading.Lock()


def reset_table_cache() -> None:
    """清空「表已建好」的记忆(测试改库/删表后用;生产不调用)。"""
    with _ready_lock:
        _ready_urls.clear()


def ensure_kb_ops_tables() -> None:
    """幂等建 kb_ops_run / kb_ops_topic 与索引;每个库每进程只真正执行一次。

    建表在独立事务里提交:若和随后的写入放同一事务,写入失败回滚会把新建的表
    一起回滚,而这里已经记成「建好了」。
    """
    url = get_settings().postgres_url
    if url in _ready_urls:
        return
    with _ready_lock:
        if url in _ready_urls:
            return
        with pooled_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_ops_run (
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
                CREATE TABLE IF NOT EXISTS kb_ops_topic (
                    topic_id           bigserial   PRIMARY KEY,
                    run_id             text        NOT NULL
                                       REFERENCES kb_ops_run(run_id) ON DELETE CASCADE,
                    rank               integer     NOT NULL,
                    query_count        integer     NOT NULL,
                    thread_count       integer     NOT NULL,
                    near_miss_count    integer     NOT NULL,
                    best_vec_score     real,
                    first_seen         timestamptz NOT NULL,
                    last_seen          timestamptz NOT NULL,
                    sample_query_ids   bigint[]    NOT NULL DEFAULT '{}',
                    triage_status      text        NOT NULL
                                       CHECK (triage_status IN (
                                           'done', 'failed', 'unsafe_samples',
                                           'out_of_budget', 'not_selected')),
                    scope              text
                                       CHECK (scope IN (
                                           'official_knowledge', 'personal_data',
                                           'out_of_scope', 'chitchat')),
                    topic              text,
                    canonical_question text,
                    reason             text,
                    UNIQUE (run_id, rank)
                )
                """
            )
            # 运行列表与「最近一次成功的运行」都按开始时间倒序取
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_ops_run_started "
                "ON kb_ops_run (started_at DESC)"
            )
        _ready_urls.add(url)


def try_acquire_run_lock() -> psycopg.Connection[Any] | None:
    """抢「正在运行」的锁。抢到返回持锁连接,抢不到(已有运行在跑)返回 None。

    调用方在运行结束后 close 这条连接即释放锁。连接不走连接池:锁绑定在会话上,
    还回池里的连接仍然持锁。
    """
    conn = psycopg.connect(get_settings().postgres_url, autocommit=True)
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))", (_RUN_LOCK_NAME,)
        ).fetchone()
    except Exception:
        conn.close()
        raise
    if row and row[0]:
        return conn
    conn.close()
    return None


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
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        conn.execute(
            "UPDATE kb_ops_run SET status = 'failed', error = %s, finished_at = now() "
            "WHERE status = 'running'",
            (_INTERRUPTED_ERROR,),
        )
        conn.execute(
            """
            INSERT INTO kb_ops_run
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
    topics: Sequence[TopicRow],
) -> None:
    """运行成功:主题与运行状态在一个事务里落库,清单要么完整出现、要么不出现。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        for t in topics:
            conn.execute(
                """
                INSERT INTO kb_ops_topic
                    (run_id, rank, query_count, thread_count, near_miss_count,
                     best_vec_score, first_seen, last_seen, sample_query_ids,
                     triage_status, scope, topic, canonical_question, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::bigint[], %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    t.rank,
                    t.query_count,
                    t.thread_count,
                    t.near_miss_count,
                    t.best_vec_score,
                    t.first_seen,
                    t.last_seen,
                    list(t.sample_query_ids),
                    t.triage_status,
                    t.scope,
                    t.topic,
                    t.canonical_question,
                    t.reason,
                ),
            )
        conn.execute(
            """
            UPDATE kb_ops_run
            SET status = 'succeeded', stats = %s, prompt_versions = %s, finished_at = now()
            WHERE run_id = %s
            """,
            (Jsonb(stats), Jsonb(prompt_versions), run_id),
        )


def fail_run(run_id: str, error: str) -> None:
    """运行失败:记下面向管理员的失败原因。error 由调用方保证不含内部细节。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        conn.execute(
            "UPDATE kb_ops_run SET status = 'failed', error = %s, finished_at = now() "
            "WHERE run_id = %s",
            (error, run_id),
        )


def purge_old_runs(keep: int) -> int:
    """只留最近 keep 次运行(按开始时间),更早的连同主题删除。返回删掉的运行数。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        cur = conn.execute(
            """
            DELETE FROM kb_ops_run
            WHERE run_id NOT IN (
                SELECT run_id FROM kb_ops_run ORDER BY started_at DESC, run_id DESC LIMIT %s
            )
            """,
            (keep,),
        )
        return cur.rowcount


def list_runs(*, limit: int = 20) -> list[dict[str, Any]]:
    """最近的运行,新的在前。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        rows = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM kb_ops_run "
            "ORDER BY started_at DESC, run_id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM kb_ops_run WHERE run_id = %s", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def latest_succeeded_run() -> dict[str, Any] | None:
    """最近一次成功的运行;一次都没成功过返回 None。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM kb_ops_run WHERE status = 'succeeded' "
            "ORDER BY started_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def list_topics(run_id: str) -> list[dict[str, Any]]:
    """一次运行的全部主题,按 rank 升序。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        rows = conn.execute(
            f"SELECT {_TOPIC_COLUMNS} FROM kb_ops_topic WHERE run_id = %s ORDER BY rank",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]
