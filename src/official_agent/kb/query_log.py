"""检索日志:对话里每查一次知识库,落一行 kb_query_log。

用来回答「哪些问题知识库答不上、为什么答不上」。关键是记下过门槛**之前**的
最高分:同样是没命中,最高分贴着门槛多半是用词没对上(该补别名),最高分很低
是库里根本没有(该补条目)。设计见 docs/proposals/kb-ops-loop.md。

约束:
- 观测面,fail-open(ADR-0005):写入在后台线程里做,失败只告警,不影响检索与对话
- query 是模型据用户原话改写的完整问题,可能带手机号/学号,落库前过 mask_pii
- 每行带 thread_id:用户删会话、会话过期清理时按它一并删除,保留期跟随对话日志
- trace_id 与 agent_conversation_log.trace_id 同值(轮级入口 id),两表按它关联
"""

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from official_agent.config import get_settings
from official_agent.security.pii import mask_pii
from official_agent.state.pools import pooled_conn

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class QueryLogEntry:
    """一次检索留下的信号。

    best_vec_score / best_kw_ratio 是过门槛之前的最高分(含义见
    kb.store.SearchOutcome);检索本身失败(status=unavailable)时没有分数,为 None。
    cycle_ids 是这次检索生效的周期过滤值,空表示只查了常青条目。
    """

    thread_id: str
    trace_id: str
    query: str
    status: str  # STATUS_OK | STATUS_NOT_FOUND | STATUS_UNAVAILABLE
    best_vec_score: float | None = None
    best_kw_ratio: float | None = None
    hit_source_ids: tuple[str, ...] = ()
    cycle_ids: tuple[int, ...] = ()


#: 已建好表的库 URL。按 URL 记:集成测试会把 URL 指到临时库。
_ready_urls: set[str] = set()
_ready_lock = threading.Lock()

# fire-and-forget 写入任务的强引用集:事件循环只持弱引用,不留在集里的任务
# 可能在完成前被回收而中途取消;完成后经回调出集。
_background_tasks: set[asyncio.Task[None]] = set()


def reset_table_cache() -> None:
    """清空「表已建好」的记忆(测试改库/删表后用;生产不调用)。"""
    with _ready_lock:
        _ready_urls.clear()


def ensure_query_log_table() -> None:
    """幂等建 kb_query_log 表与索引;每个库每进程只真正执行一次。

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
                CREATE TABLE IF NOT EXISTS kb_query_log (
                    id             bigserial   PRIMARY KEY,
                    thread_id      text        NOT NULL,
                    trace_id       text        NOT NULL,
                    query          text        NOT NULL,
                    status         text        NOT NULL
                                   CHECK (status IN ('ok', 'not_found', 'unavailable')),
                    best_vec_score real,
                    best_kw_ratio  real,
                    hit_source_ids text[]      NOT NULL DEFAULT '{}',
                    cycle_ids      integer[]   NOT NULL DEFAULT '{}',
                    created_at     timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            # 三个访问路径各一个索引:按时间窗聚合、按会话删除、按 trace 关联对话日志
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_query_log_created "
                "ON kb_query_log (created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_query_log_thread ON kb_query_log (thread_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_query_log_trace ON kb_query_log (trace_id)"
            )
        _ready_urls.add(url)


def write_query(entry: QueryLogEntry) -> None:
    """同步落一行(阻塞 IO,异步调用方走 record_query)。query 在这里脱敏。"""
    ensure_query_log_table()
    with pooled_conn() as conn:
        conn.execute(
            """
            INSERT INTO kb_query_log
                (thread_id, trace_id, query, status, best_vec_score, best_kw_ratio,
                 hit_source_ids, cycle_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s::text[], %s::int[])
            """,
            (
                entry.thread_id,
                entry.trace_id,
                mask_pii(entry.query),
                entry.status,
                entry.best_vec_score,
                entry.best_kw_ratio,
                list(entry.hit_source_ids),
                list(entry.cycle_ids),
            ),
        )


def record_query(entry: QueryLogEntry) -> None:
    """后台落一行,立即返回,不抛异常;必须在事件循环里调用。"""

    async def _write() -> None:
        try:
            await asyncio.to_thread(write_query, entry)
        except Exception:  # noqa: BLE001
            # fail-open(ADR-0005):丢的只是一行观测数据,检索结果已经返回给模型,
            # 对话不受影响;代价是这次检索不进运营统计
            logger.warning(
                "kb_query_log 写入失败(已忽略) status=%s thread_id=%s",
                entry.status,
                entry.thread_id,
                exc_info=True,
            )

    task = asyncio.create_task(_write())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def list_unanswered(*, since: datetime, until: datetime, limit: int) -> list[dict[str, Any]]:
    """时间窗 [since, until) 内没命中(status=not_found)的检索,按 (created_at, id) 升序。

    每行含 id / thread_id / query / best_vec_score / created_at。超过 limit 时只留
    最新的 limit 行——离线聚类要给嵌入与计算量封顶,而新近的问题更值得先处理。
    """
    ensure_query_log_table()
    with pooled_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, thread_id, query, best_vec_score, created_at
            FROM (
                SELECT id, thread_id, query, best_vec_score, created_at
                FROM kb_query_log
                WHERE status = 'not_found' AND created_at >= %s AND created_at < %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
            ) newest
            ORDER BY created_at, id
            """,
            (since, until, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def summarize_window(*, since: datetime, until: datetime, near_miss_floor: float) -> dict[str, int]:
    """时间窗 [since, until) 内各结局的检索次数:total / ok / not_found / unavailable / near_miss。

    near_miss 是 not_found 里最高向量分不低于 near_miss_floor 的次数(差一点命中)。
    """
    ensure_query_log_table()
    with pooled_conn() as conn:
        row = conn.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'ok') AS ok,
                   count(*) FILTER (WHERE status = 'not_found') AS not_found,
                   count(*) FILTER (WHERE status = 'unavailable') AS unavailable,
                   count(*) FILTER (
                       WHERE status = 'not_found' AND best_vec_score >= %s
                   ) AS near_miss
            FROM kb_query_log
            WHERE created_at >= %s AND created_at < %s
            """,
            (near_miss_floor, since, until),
        ).fetchone()
    keys = ("total", "ok", "not_found", "unavailable", "near_miss")
    return {key: int(row[key]) if row else 0 for key in keys}


def get_queries(query_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """按日志行 id 取问题文本与时间,返回 {id: {query, created_at}}。

    所在会话已被删除的行查不到,结果里就没有那个 id——引用这些行的一方(主题清单的
    样本)据此自然随会话删除而消失。
    """
    if not query_ids:
        return {}
    ensure_query_log_table()
    with pooled_conn() as conn:
        rows = conn.execute(
            "SELECT id, query, created_at FROM kb_query_log WHERE id = ANY(%s::bigint[])",
            (list(query_ids),),
        ).fetchall()
    return {int(r["id"]): {"query": r["query"], "created_at": r["created_at"]} for r in rows}


def delete_thread_queries(thread_id: str) -> int:
    """删除一个会话的全部检索日志,返回删除行数。

    失败照常抛出:删会话的调用方要据此如实报告「没删干净」,不能当作已删。
    """
    ensure_query_log_table()
    with pooled_conn() as conn:
        cur = conn.execute("DELETE FROM kb_query_log WHERE thread_id = %s", (thread_id,))
        return cur.rowcount
