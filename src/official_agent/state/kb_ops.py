"""知识库运营的状态面:运行记录 kb_ops_run、主题清单 kb_ops_topic、草稿 kb_draft。

一次离线挖掘是一行 kb_ops_run,它产出的主题是若干行 kb_ops_topic(随运行级联删除)。
草稿是 agent 的私有状态,不是知识库条目:它带着证据、审核结论和审核状态,人在管理面
批准之后才由发布逻辑写进知识库。草稿不随运行删除——运行只留最近几次,而待审的
草稿要一直等到有人处理。

约束:
- 主题行不存用户问题的文本,只存检索日志行的 id(sample_query_ids),展示时回查
  kb_query_log:用户删了会话,那几条样本也就查不到了。模型写的主题名、标准问法与
  理由是对多人提问的概括,随运行保留
- 只保留最近若干次运行(purge_old_runs),更早的连同主题一起删
- 同一时刻只允许一次运行:会话级 advisory lock,持锁连接一断开锁就释放,进程崩溃
  不会留下死锁;崩溃留下的 running 行在下一次运行开始时改记为 failed
- 草稿的状态只能从 pending 走向 approved / rejected / superseded,且只走一次
  (decide_draft 按 status = 'pending' 条件更新);同一份草稿的发布由 advisory lock 串行化
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

#: 主题的起草结局(kb_ops_topic.draft_status;没走到起草的主题为空)。
#: drafted 有草稿待审;auto_rejected 草稿与原文矛盾,被代码驳回;failed 起草或审核
#: 调用失败;out_of_budget 预算用完;duplicate 与已有问法或同批草稿重复,没有产出
DRAFT_READY = "drafted"
DRAFT_AUTO_REJECTED = "auto_rejected"
DRAFT_FAILED = "failed"
DRAFT_OUT_OF_BUDGET = "out_of_budget"
DRAFT_DUPLICATE = "duplicate"

#: 草稿的审核状态(kb_draft.status)
DRAFT_PENDING = "pending"
DRAFT_APPROVED = "approved"
DRAFT_REJECTED = "rejected"
DRAFT_SUPERSEDED = "superseded"

#: advisory lock 的名字;库内唯一即可,取哈希后作为锁键
_RUN_LOCK_NAME = "official_agent.kb_ops.run"
_DRAFT_LOCK_PREFIX = "official_agent.kb_ops.draft:"
_INTERRUPTED_ERROR = "进程中断,运行没有完成"

_RUN_COLUMNS = (
    "run_id, status, window_start, window_end, triggered_by, params, stats, "
    "prompt_versions, error, started_at, finished_at"
)
_TOPIC_COLUMNS = (
    "topic_id, run_id, rank, query_count, thread_count, near_miss_count, best_vec_score, "
    "first_seen, last_seen, sample_query_ids, triage_status, scope, topic, "
    "canonical_question, reason, draft_status"
)
_DRAFT_COLUMNS = (
    "draft_id, run_id, topic_rank, kind, topic, question, answer, evidence, "
    "target_source_id, cycle_id, conflict_with, support, review, prompt_versions, status, "
    "edited_by, decided_by, decided_at, published_source_id, created_at, updated_at"
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
    draft_status: str | None = None


@dataclass(frozen=True)
class DraftRow:
    """待落库的一份草稿。

    evidence 是逐句出处 [{sentence, source_id, source_title, chunk_id, quote}];
    support 是主题热度与样本引用 {query_count, thread_count, near_miss_count,
    sample_query_ids}(样本只存检索日志行 id);review 是代码复核后的逐句结论等。
    cycle_id 是发布时条目要绑定的招新周期(证据来自绑定了周期的条目时跟随它),
    常青为 None。status 缺省 pending;被代码驳回的草稿以 rejected 落库,留给人看
    它错在哪。
    """

    topic_rank: int
    kind: str
    topic: str
    question: str
    answer: str
    evidence: list[dict[str, Any]]
    target_source_id: str | None
    conflict_with: tuple[str, ...]
    support: dict[str, Any]
    review: dict[str, Any]
    prompt_versions: dict[str, str]
    cycle_id: int | None = None
    status: str = DRAFT_PENDING


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
                    draft_status       text
                                       CHECK (draft_status IN (
                                           'drafted', 'auto_rejected', 'failed',
                                           'out_of_budget', 'duplicate')),
                    UNIQUE (run_id, rank)
                )
                """
            )
            # draft_status 是增量列,存量库幂等补齐(没走到起草的主题为空)
            conn.execute(
                """
                ALTER TABLE kb_ops_topic ADD COLUMN IF NOT EXISTS draft_status text
                    CHECK (draft_status IN (
                        'drafted', 'auto_rejected', 'failed', 'out_of_budget', 'duplicate'))
                """
            )
            # run_id 不设外键:运行只留最近几次,草稿要留到有人处理之后
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kb_draft (
                    draft_id            bigserial   PRIMARY KEY,
                    run_id              text        NOT NULL,
                    topic_rank          integer     NOT NULL,
                    kind                text        NOT NULL
                                        CHECK (kind IN ('alt_question', 'new_faq', 'missing')),
                    topic               text        NOT NULL,
                    question            text        NOT NULL,
                    answer              text        NOT NULL DEFAULT '',
                    evidence            jsonb       NOT NULL DEFAULT '[]',
                    target_source_id    text,
                    cycle_id            integer,
                    conflict_with       text[]      NOT NULL DEFAULT '{}',
                    support             jsonb       NOT NULL DEFAULT '{}',
                    review              jsonb       NOT NULL DEFAULT '{}',
                    prompt_versions     jsonb       NOT NULL DEFAULT '{}',
                    status              text        NOT NULL DEFAULT 'pending'
                                        CHECK (status IN (
                                            'pending', 'approved', 'rejected', 'superseded')),
                    edited_by           integer,
                    decided_by          integer,
                    decided_at          timestamptz,
                    published_source_id text,
                    created_at          timestamptz NOT NULL DEFAULT now(),
                    updated_at          timestamptz NOT NULL DEFAULT now(),
                    UNIQUE (run_id, topic_rank)
                )
                """
            )
            # 待审列表按状态筛、新的在前
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_draft_status "
                "ON kb_draft (status, created_at DESC)"
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
    drafts: Sequence[DraftRow] = (),
    superseded_draft_ids: Sequence[int] = (),
) -> None:
    """运行成功:主题、草稿与运行状态在一个事务里落库,要么完整出现、要么都不出现。

    superseded_draft_ids 是被本次新草稿顶替的旧草稿:仍在待审的改记为 superseded,
    已经有人处理过的不动。
    """
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        for t in topics:
            conn.execute(
                """
                INSERT INTO kb_ops_topic
                    (run_id, rank, query_count, thread_count, near_miss_count,
                     best_vec_score, first_seen, last_seen, sample_query_ids,
                     triage_status, scope, topic, canonical_question, reason, draft_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::bigint[], %s, %s, %s, %s, %s, %s)
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
                    t.draft_status,
                ),
            )
        if superseded_draft_ids:
            conn.execute(
                "UPDATE kb_draft SET status = 'superseded', decided_at = now(), "
                "updated_at = now() WHERE draft_id = ANY(%s::bigint[]) AND status = 'pending'",
                (list(superseded_draft_ids),),
            )
        for d in drafts:
            conn.execute(
                """
                INSERT INTO kb_draft
                    (run_id, topic_rank, kind, topic, question, answer, evidence,
                     target_source_id, cycle_id, conflict_with, support, review,
                     prompt_versions, status, decided_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::text[], %s, %s, %s, %s,
                        CASE WHEN %s::text = 'pending' THEN NULL ELSE now() END)
                """,
                (
                    run_id,
                    d.topic_rank,
                    d.kind,
                    d.topic,
                    d.question,
                    d.answer,
                    Jsonb(d.evidence),
                    d.target_source_id,
                    d.cycle_id,
                    list(d.conflict_with),
                    Jsonb(d.support),
                    Jsonb(d.review),
                    Jsonb(d.prompt_versions),
                    d.status,
                    d.status,
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
    """一次运行的全部主题,按 rank 升序。每行带 draft_id:这个主题产出的草稿,没有为空。"""
    ensure_kb_ops_tables()
    columns = ", ".join(f"t.{c.strip()}" for c in _TOPIC_COLUMNS.split(","))
    with pooled_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT {columns}, d.draft_id
            FROM kb_ops_topic t
            LEFT JOIN kb_draft d ON d.run_id = t.run_id AND d.topic_rank = t.rank
            WHERE t.run_id = %s
            ORDER BY t.rank
            """,
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── 草稿 ────────────────────────────────────────────────


def try_acquire_draft_lock(draft_id: int) -> psycopg.Connection[Any] | None:
    """抢「正在发布这份草稿」的锁,用法同 try_acquire_run_lock。

    两个管理员同时批准同一份草稿时,后到的抢不到锁,不会各发布一遍。
    """
    conn = psycopg.connect(get_settings().postgres_url, autocommit=True)
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            (f"{_DRAFT_LOCK_PREFIX}{draft_id}",),
        ).fetchone()
    except Exception:
        conn.close()
        raise
    if row and row[0]:
        return conn
    conn.close()
    return None


def list_pending_draft_questions() -> list[tuple[int, str]]:
    """全部待审草稿的 (draft_id, 问题),旧的在前;查重时用来找该被顶替的旧草稿。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        rows = conn.execute(
            "SELECT draft_id, question FROM kb_draft WHERE status = 'pending' ORDER BY draft_id"
        ).fetchall()
    return [(int(r["draft_id"]), str(r["question"])) for r in rows]


def list_drafts(
    *, status: str | None = None, kind: str | None = None, page: int = 1, size: int = 20
) -> dict[str, Any]:
    """草稿分页列表,新的在前。返回 {items, total, page, size}。"""
    ensure_kb_ops_tables()
    where = ["TRUE"]
    params: list[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    clause = " AND ".join(where)
    with pooled_conn() as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM kb_draft WHERE {clause}", params
        ).fetchone()
        rows = conn.execute(
            f"SELECT {_DRAFT_COLUMNS} FROM kb_draft WHERE {clause} "
            "ORDER BY created_at DESC, draft_id DESC LIMIT %s OFFSET %s",
            [*params, size, (page - 1) * size],
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": int(total["n"]) if total else 0,
        "page": page,
        "size": size,
    }


def get_draft(draft_id: int) -> dict[str, Any] | None:
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        row = conn.execute(
            f"SELECT {_DRAFT_COLUMNS} FROM kb_draft WHERE draft_id = %s", (draft_id,)
        ).fetchone()
    return dict(row) if row else None


def update_draft_content(
    draft_id: int, *, question: str, answer: str, cycle_id: int | None, edited_by: int
) -> bool:
    """管理员在批准前改草稿的问题、答案与绑定周期。只有待审的草稿能改;改不了返回 False。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        cur = conn.execute(
            "UPDATE kb_draft SET question = %s, answer = %s, cycle_id = %s, edited_by = %s, "
            "updated_at = now() WHERE draft_id = %s AND status = 'pending'",
            (question, answer, cycle_id, edited_by, draft_id),
        )
        return cur.rowcount > 0


def decide_draft(
    draft_id: int, *, status: str, decided_by: int, published_source_id: str | None = None
) -> bool:
    """把待审草稿改成最终状态。草稿已经不是待审(别人先处理了)返回 False,不覆盖。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        cur = conn.execute(
            "UPDATE kb_draft SET status = %s, decided_by = %s, decided_at = now(), "
            "published_source_id = %s, updated_at = now() "
            "WHERE draft_id = %s AND status = 'pending'",
            (status, decided_by, published_source_id, draft_id),
        )
        return cur.rowcount > 0


def purge_decided_drafts(older_than_days: int) -> int:
    """删除处理完(非待审)且超过保留天数的草稿,返回删除行数。待审的草稿不删。"""
    ensure_kb_ops_tables()
    with pooled_conn() as conn:
        cur = conn.execute(
            "DELETE FROM kb_draft WHERE status <> 'pending' "
            "AND decided_at < now() - make_interval(days => %s)",
            (older_than_days,),
        )
        return cur.rowcount
