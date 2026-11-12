"""KB 数据面:知识条目 CRUD 与混合检索。

- SQL 同步(threads.py 先例:mock 解耦单测,真库往返走集成档);
  异步入口用 asyncio.to_thread 包装,不阻塞事件循环
- 连接走进程级连接池;建表(ensure_kb_schema)每进程只跑一次、单独提交——
  no-op 的 DDL 也会在事务里持表锁,放进每次检索会让检索与入库互相堵
- 入库单事务原子:source + 内容表 + chunks 同生共死
- kind=test 条目可入库但不进生产检索(search 默认排除;eval 显式 include_test)
- 检索只回 enabled 且 model_version=当前 meta.version 的块(禁混排的读侧配套)
- 检索 = 向量一路 + 关键词一路,RRF 合并;低于相似度门槛且字面也对不上的
  候选直接丢掉——「查不到」必须能真的返回空,否则模型会拿不相关片段硬凑答案
- 绑定了招新周期的条目(kb_source.cycle_id)只在调用方给出该周期时才进检索;
  不给周期只查常青条目——宁可查不到,也不拿往届的时间地点作答
- FAQ 可以带附加问法(alt_questions):每种问法各自成块参与检索,但同一条 FAQ
  在一次检索结果里只占一个名额
"""

import asyncio
import secrets
import threading
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from official_agent.config import get_settings
from official_agent.kb.chunking import KbChunk, embed_text, split_doc, split_faq
from official_agent.kb.embedding import Embedder, embed_query, embed_texts
from official_agent.kb.keywords import query_terms
from official_agent.kb.schema import current_embed_target, ensure_kb_schema

_SOURCE_COLUMNS = (
    "source_id, source_title, source_type, kind, tags, enabled, updated_by, cycle_id, "
    "created_at, updated_at"
)


class KbValidationError(ValueError):
    """条目内容不满足入库要求(管理面映射为 400)。"""


@dataclass(frozen=True)
class SourceInput:
    """新建/更新一条知识的输入。type=faq 用 question/answer;type=doc 用 content_md。

    cycle_id 为 None 表示常青条目;填招新周期 id 表示只在该周期开放期间可被检索。
    alt_questions 只对 FAQ 有意义:同一个答案的其他问法。
    """

    title: str
    type: str  # 'faq' | 'doc'
    kind: str = "normal"  # 'normal' | 'test'
    tags: tuple[str, ...] = ()
    question: str = ""
    answer: str = ""
    content_md: str = ""
    updated_by: str = ""
    cycle_id: int | None = None
    alt_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    title: str
    type: str
    kind: str
    tags: list[str]
    enabled: bool
    updated_by: str
    question: str = ""
    answer: str = ""
    content_md: str = ""
    chunk_count: int = 0
    cycle_id: int | None = None
    alt_questions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SearchHit:
    """一条检索命中。source_id+title 是引用锚;chunk_text 供上下文/摘要。

    score 是向量相似度(0-1);keyword_score 是问题关键词在该块里字面出现的比例。
    chunk_id 定位到具体的块(需要逐句对出处的调用方用);source_type 是 faq / doc。
    """

    source_id: str
    title: str
    chunk_text: str
    heading_path: str
    score: float
    keyword_score: float = 0.0
    chunk_id: str = ""
    source_type: str = ""


@dataclass(frozen=True)
class ChunkRecord:
    """一个块的原文与它所属条目的标识。cycle_id 是所属条目绑定的招新周期,常青为 None。"""

    chunk_id: str
    source_id: str
    title: str
    source_type: str
    text: str
    heading_path: str
    cycle_id: int | None = None


@dataclass(frozen=True)
class SearchOutcome:
    """一次检索的命中,加上过门槛**之前**的最高分(检索日志的原料)。

    best_vec_score / best_kw_ratio 取全部候选里的最高值,不管候选有没有过门槛:
    没命中时靠它区分「差一点就命中」(多半是用词没对上)与「库里根本没有」。
    没有任何候选时两者都是 None;问题里抽不出关键词时 best_kw_ratio 是 None。
    """

    hits: list[SearchHit]
    best_vec_score: float | None = None
    best_kw_ratio: float | None = None


_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def _conn() -> AbstractContextManager[psycopg.Connection[dict[str, Any]]]:
    """签出一条池化连接——返回上下文管理器,必须 with。

    池按 postgres_url 分开建:集成测试会把 URL 指到临时库,共用一个池会连错库。
    """
    url = get_settings().postgres_url
    pool = _pools.get(url)
    if pool is None:
        with _pools_lock:
            pool = _pools.get(url)
            if pool is None:
                pool = ConnectionPool(
                    url, min_size=1, max_size=4, open=True, kwargs={"row_factory": dict_row}
                )
                _pools[url] = pool
    # ignore:池的泛型推断不到 kwargs 里的 dict_row,实际签出的就是 dict_row 连接
    return pool.connection()  # type: ignore[return-value]


def reset_pools() -> None:
    """关闭全部连接池并清空建表缓存(测试收尾/改 URL 后用;生产不调用)。"""
    with _pools_lock:
        for pool in _pools.values():
            pool.close()
        _pools.clear()
    reset_schema_cache()


#: 最近一次建表的 (库 URL, 模型, 维度) 与得到的 meta.version。只记一份:换模型再
#: 换回来时必须重跑 ensure(版本已经 +2),按键缓存多份会拿到过期版本。
_schema_state: tuple[tuple[str, str, int], int] | None = None
_schema_lock = threading.Lock()


def reset_schema_cache() -> None:
    global _schema_state
    with _schema_lock:
        _schema_state = None


def _schema_version(*, refresh: bool = False) -> int:
    """当前生效的向量版本。检索走缓存,首次调用(或换了模型/维度)才真正跑建表;
    入库传 refresh=True 每次都跑——入库是管理员低频操作,持锁无所谓,而它能
    及时发现库里 meta 与配置不一致(换模型)并清掉旧向量。

    建表在**独立事务**里提交:若和随后的写入放在同一事务,写入失败回滚会把
    新建的表一起回滚,而缓存里已经记成「建好了」。
    """
    global _schema_state
    model, dim = current_embed_target()
    key = (get_settings().postgres_url, model, dim)
    state = _schema_state
    if not refresh and state is not None and state[0] == key:
        return state[1]
    with _schema_lock:
        state = _schema_state
        if not refresh and state is not None and state[0] == key:
            return state[1]
        with _conn() as conn:
            version = ensure_kb_schema(conn, embed_model=model, dim=dim)
        _schema_state = (key, version)
        return version


def new_source_id() -> str:
    return f"kb_{secrets.token_hex(6)}"


def _split_chunks(source: SourceInput) -> list[KbChunk]:
    if source.kind not in ("normal", "test"):
        raise KbValidationError(f"kind 只支持 normal/test,收到 {source.kind!r}")
    if source.type == "faq":
        if not source.question.strip() or not source.answer.strip():
            raise KbValidationError("FAQ 条目必须同时有 question 与 answer")
        return split_faq(source.question, source.answer, clean_alt_questions(source))
    if source.type == "doc":
        if not source.content_md.strip():
            raise KbValidationError("正文条目 content_md 不能为空")
        return split_doc(source.content_md)
    raise KbValidationError(f"source_type 只支持 faq/doc,收到 {source.type!r}")


def same_question(a: str, b: str) -> bool:
    """两种问法字面上是不是同一句(忽略空白与大小写)。"""
    return "".join(a.split()).lower() == "".join(b.split()).lower()


def clean_alt_questions(source: SourceInput) -> list[str]:
    """附加问法去掉空的、与主问法相同的、彼此重复的,保持原有顺序。"""
    kept: list[str] = []
    for alt in source.alt_questions:
        text = alt.strip()
        if not text or same_question(text, source.question):
            continue
        if any(same_question(text, seen) for seen in kept):
            continue
        kept.append(text)
    return kept


async def ingest_source(
    source: SourceInput, *, embedder: Embedder | None = None, source_id: str | None = None
) -> str:
    """新增/更新一条知识并重建其 chunks(条目级增量)。返回 source_id。

    embedder 可注入(测试/换端点);缺省走 EMBED_* 配置的托管端点。
    """
    chunks = _split_chunks(source)
    do_embed = embedder or embed_texts
    vectors = await do_embed([embed_text(c) for c in chunks])
    return await asyncio.to_thread(_write_source, source, chunks, vectors, source_id)


def _write_source(
    source: SourceInput, chunks: list[KbChunk], vectors: list[list[float]], source_id: str | None
) -> str:
    if len(vectors) != len(chunks):
        raise RuntimeError(f"向量数 {len(vectors)} 与块数 {len(chunks)} 不一致")
    version = _schema_version(refresh=True)
    sid = source_id or new_source_id()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO kb_source
                (source_id, source_title, source_type, kind, tags, updated_by, cycle_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                source_title = EXCLUDED.source_title,
                source_type = EXCLUDED.source_type,
                kind = EXCLUDED.kind,
                tags = EXCLUDED.tags,
                updated_by = EXCLUDED.updated_by,
                cycle_id = EXCLUDED.cycle_id,
                updated_at = now()
            """,
            (
                sid,
                source.title,
                source.type,
                source.kind,
                list(source.tags),
                source.updated_by,
                source.cycle_id,
            ),
        )
        # 内容两表都清再插:类型迁移(faq↔doc)天然正确
        conn.execute("DELETE FROM kb_faq WHERE source_id = %s", (sid,))
        conn.execute("DELETE FROM kb_doc WHERE source_id = %s", (sid,))
        if source.type == "faq":
            conn.execute(
                "INSERT INTO kb_faq (source_id, question, answer, alt_questions) "
                "VALUES (%s, %s, %s, %s::text[])",
                (sid, source.question, source.answer, clean_alt_questions(source)),
            )
        else:
            conn.execute(
                "INSERT INTO kb_doc (source_id, content_md) VALUES (%s, %s)",
                (sid, source.content_md),
            )
        conn.execute("DELETE FROM kb_chunks WHERE source_id = %s", (sid,))
        for chunk, vector in zip(chunks, vectors, strict=True):
            conn.execute(
                """
                INSERT INTO kb_chunks
                    (chunk_id, source_id, chunk_ordinal, chunk_text, heading_path,
                     embedding, model_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    f"{sid}#{chunk.ordinal}",
                    sid,
                    chunk.ordinal,
                    chunk.text,
                    chunk.heading_path,
                    _to_pgvector(vector),
                    version,
                ),
            )
    return sid


def set_source_enabled(source_id: str, enabled: bool, *, updated_by: str) -> bool:
    """启/停用。停用立即退出生检索(读侧 enabled 过滤),chunks 保留可复活。"""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE kb_source SET enabled = %s, updated_by = %s, updated_at = now() "
            "WHERE source_id = %s",
            (enabled, updated_by, source_id),
        )
        return cur.rowcount > 0


def delete_source(source_id: str) -> bool:
    """物理删除(source 为主键方,faq/doc/chunks 级联)。"""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM kb_source WHERE source_id = %s", (source_id,))
        return cur.rowcount > 0


def get_source(source_id: str) -> SourceRecord | None:
    """详情:source + 内容 + 块数。不存在返回 None。"""
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM kb_source WHERE source_id = %s", (source_id,)
        ).fetchone()
        if row is None:
            return None
        content = _fetch_content(conn, source_id)
        count = conn.execute(
            "SELECT count(*) AS n FROM kb_chunks WHERE source_id = %s", (source_id,)
        ).fetchone()
    return _record(row, content, chunk_count=count["n"] if count else 0)


def list_sources(
    *, page: int = 1, size: int = 20, kind: str | None = None, keyword: str | None = None
) -> dict:
    """分页列表(管理面板数据源)。keyword 匹配标题。返回 {items,total,page,size}。"""
    where = ["TRUE"]
    params: list[Any] = []
    if kind in ("normal", "test"):
        where.append("kind = %s")
        params.append(kind)
    if keyword:
        where.append("source_title ILIKE %s")
        params.append(f"%{keyword}%")
    clause = " AND ".join(where)
    with _conn() as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM kb_source WHERE {clause}", params
        ).fetchone()
        rows = conn.execute(
            f"""
            SELECT {_SOURCE_COLUMNS}, (
                SELECT count(*) FROM kb_chunks c WHERE c.source_id = kb_source.source_id
            ) AS chunk_count
            FROM kb_source WHERE {clause}
            ORDER BY updated_at DESC
            LIMIT %s OFFSET %s
            """,
            [*params, size, (page - 1) * size],
        ).fetchall()
    empty_content: dict[str, Any] = {"question": "", "answer": "", "content_md": ""}
    items = [
        _record(r, empty_content, chunk_count=r["chunk_count"]) for r in rows
    ]
    return {"items": items, "total": total["n"] if total else 0, "page": page, "size": size}


#: 每一路先取这么多候选再合并;合并后再按门槛过滤、截 top_k。
_CANDIDATES = 12
#: RRF 平滑常数(常用取值 60):排名靠前的差距被压平,两路都靠前的胜出。
_RRF_K = 60


async def search(
    query: str,
    *,
    top_k: int = 4,
    include_test: bool = False,
    embedder: Embedder | None = None,
    min_score: float | None = None,
    cycle_ids: Sequence[int] = (),
) -> list[SearchHit]:
    """混合检索 top-k。默认排除 kind=test;都不达标返回空表(降级话术归调用方)。

    min_score 缺省取配置 kb_min_score;传 0 等于关掉门槛(评测看原始分布用)。
    cycle_ids 是当前开放的招新周期:常青条目始终参与,绑定了周期的条目只在
    其周期出现在这里时参与;缺省(空)只查常青条目。
    """
    outcome = await search_with_signals(
        query,
        top_k=top_k,
        include_test=include_test,
        embedder=embedder,
        min_score=min_score,
        cycle_ids=cycle_ids,
    )
    return outcome.hits


async def search_with_signals(
    query: str,
    *,
    top_k: int = 4,
    include_test: bool = False,
    embedder: Embedder | None = None,
    min_score: float | None = None,
    cycle_ids: Sequence[int] = (),
) -> SearchOutcome:
    """同 search,另带过门槛之前的最高分(见 SearchOutcome)。参数含义与 search 一致。"""
    if embedder is None:
        query_vec = await embed_query(query)
    else:
        query_vec = (await embedder([query]))[0]
    settings = get_settings()
    threshold = settings.kb_min_score if min_score is None else min_score
    return await asyncio.to_thread(
        _search_sync,
        query_vec,
        top_k,
        include_test,
        query=query,
        min_score=threshold,
        keyword_min_ratio=settings.kb_keyword_min_ratio,
        cycle_ids=cycle_ids,
    )


def _escape_like(term: str) -> str:
    """LIKE 模式里 % _ \\ 是元字符;关键词来自用户问题,必须转义成字面量。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_sync(
    query_vec: list[float],
    top_k: int,
    include_test: bool,
    *,
    query: str = "",
    min_score: float = 0.0,
    keyword_min_ratio: float = 1.0,
    cycle_ids: Sequence[int] = (),
) -> SearchOutcome:
    version = _schema_version()
    terms = query_terms(query) if query else []
    kind_filter = "" if include_test else " AND s.kind = 'normal'"
    # 空数组时 ANY 恒假,只剩 cycle_id IS NULL 的常青条目
    cycle_filter = " AND (s.cycle_id IS NULL OR s.cycle_id = ANY(%(cycle_ids)s::int[]))"
    vec = _to_pgvector(query_vec)
    # 两路候选取并集,再对并集里每个块同时算向量分与字面命中数:
    # 只出现在关键词一路的块也要有向量分,门槛与排序才可比。
    # 关键词匹配范围含标题与标题链(「部门介绍」这类标题本身就是信号)。
    with _conn() as conn:
        rows = conn.execute(
            f"""
            WITH q AS (SELECT %(vec)s::vector AS v),
            vec AS (
                SELECT c.chunk_id
                FROM kb_chunks c JOIN kb_source s USING (source_id), q
                WHERE s.enabled AND c.model_version = %(version)s{kind_filter}{cycle_filter}
                ORDER BY c.embedding <=> q.v, c.chunk_ordinal, c.source_id
                LIMIT %(candidates)s
            ),
            kw AS (
                SELECT c.chunk_id, count(*) AS matched
                FROM kb_chunks c JOIN kb_source s USING (source_id)
                JOIN unnest(%(patterns)s::text[]) AS t(pattern)
                  ON (s.source_title || ' ' || c.heading_path || ' ' || c.chunk_text)
                     ILIKE t.pattern
                WHERE s.enabled AND c.model_version = %(version)s{kind_filter}{cycle_filter}
                GROUP BY c.chunk_id
                ORDER BY matched DESC, c.chunk_id
                LIMIT %(candidates)s
            )
            SELECT s.source_id, s.source_title, s.source_type, c.chunk_id, c.chunk_text,
                   c.heading_path, c.chunk_ordinal,
                   1 - (c.embedding <=> q.v) AS score, COALESCE(kw.matched, 0) AS matched
            FROM kb_chunks c JOIN kb_source s USING (source_id) CROSS JOIN q
            LEFT JOIN kw USING (chunk_id)
            WHERE c.chunk_id IN (SELECT chunk_id FROM vec UNION SELECT chunk_id FROM kw)
            """,
            {
                "vec": vec,
                "version": version,
                "candidates": max(_CANDIDATES, top_k),
                "patterns": [f"%{_escape_like(t)}%" for t in terms],
                "cycle_ids": list(cycle_ids),
            },
        ).fetchall()
    hits = _fuse(
        rows,
        n_terms=len(terms),
        top_k=top_k,
        min_score=min_score,
        keyword_min_ratio=keyword_min_ratio,
    )
    best_vec_score, best_kw_ratio = _best_signals(rows, n_terms=len(terms))
    return SearchOutcome(hits=hits, best_vec_score=best_vec_score, best_kw_ratio=best_kw_ratio)


def _best_signals(
    rows: list[dict[str, Any]], *, n_terms: int
) -> tuple[float | None, float | None]:
    """候选里的最高向量分与最高关键词命中比,不看门槛。纯函数。

    候选是两路各取前若干名的并集,向量一路按距离排序,所以这里的最高向量分
    就是过滤条件内全部块的最高分(HNSW 是近似检索,个别时候会略低于真实最高分)。
    """
    if not rows:
        return None, None
    best_vec_score = max(float(r["score"]) for r in rows)
    if not n_terms:
        return best_vec_score, None
    best_kw_ratio = max(int(r.get("matched") or 0) for r in rows) / n_terms
    return best_vec_score, best_kw_ratio


def _fuse(
    rows: list[dict[str, Any]],
    *,
    n_terms: int,
    top_k: int,
    min_score: float,
    keyword_min_ratio: float,
) -> list[SearchHit]:
    """两路排名用 RRF 合并,再过门槛。纯函数,单测直接喂行。

    门槛是「或」:向量分达标,或问题关键词有足够比例字面出现在块里。后者兜住
    专有名词——向量对「计拔」这种词不敏感,但条目里写了它就该召回。
    同分按 (块序号, source_id) 决胜:否则同分行的顺序由 PG 计划形态决定,结果抖动。
    同一条 FAQ 的多种问法各有一块、答案相同,只留排名最前的那块,不让一条 FAQ
    占掉多个名额;正文条目的各块内容不同,不合并。
    """

    def tiebreak(r: dict[str, Any]) -> tuple[int, str]:
        return (int(r.get("chunk_ordinal") or 0), str(r["source_id"]))

    def keyword_ratio(r: dict[str, Any]) -> float:
        return int(r.get("matched") or 0) / n_terms if n_terms else 0.0

    by_vector = sorted(rows, key=lambda r: (-float(r["score"]), *tiebreak(r)))
    by_keyword = sorted(
        (r for r in rows if int(r.get("matched") or 0) > 0),
        key=lambda r: (-int(r["matched"]), -float(r["score"]), *tiebreak(r)),
    )
    rrf: dict[int, float] = {}
    for rank, r in enumerate(by_vector, start=1):
        rrf[id(r)] = 1 / (_RRF_K + rank)
    for rank, r in enumerate(by_keyword, start=1):
        rrf[id(r)] = rrf.get(id(r), 0.0) + 1 / (_RRF_K + rank)

    passed = [
        r
        for r in rows
        if float(r["score"]) >= min_score or (n_terms and keyword_ratio(r) >= keyword_min_ratio)
    ]
    passed.sort(key=lambda r: (-rrf[id(r)], -float(r["score"]), *tiebreak(r)))
    kept: list[dict[str, Any]] = []
    faq_sources_seen: set[str] = set()
    for r in passed:
        if r.get("source_type") == "faq":
            if r["source_id"] in faq_sources_seen:
                continue
            faq_sources_seen.add(r["source_id"])
        kept.append(r)
    return [
        SearchHit(
            source_id=r["source_id"],
            title=r["source_title"],
            chunk_text=r["chunk_text"],
            heading_path=r["heading_path"],
            score=float(r["score"]),
            keyword_score=round(keyword_ratio(r), 3),
            chunk_id=str(r.get("chunk_id") or ""),
            source_type=str(r.get("source_type") or ""),
        )
        for r in kept[:top_k]
    ]


#: 块读取的可见性条件,与检索一致:条目启用、不是评测条目、向量版本是当前的、
#: 常青或绑定在给定的开放周期上
_VISIBLE_CHUNK = (
    "s.enabled AND s.kind = 'normal' AND c.model_version = %(version)s "
    "AND (s.cycle_id IS NULL OR s.cycle_id = ANY(%(cycle_ids)s::int[]))"
)
_CHUNK_COLUMNS = (
    "c.chunk_id, c.source_id, s.source_title, s.source_type, s.cycle_id, c.chunk_text, "
    "c.heading_path"
)


def get_chunks(
    chunk_ids: Sequence[str], *, cycle_ids: Sequence[int] = ()
) -> dict[str, ChunkRecord]:
    """按块 id 取原文,返回 {chunk_id: ChunkRecord}。

    只回当前检索得到的块(条件与 search 相同,cycle_ids 含义也相同):条目已停用、
    是评测条目、不在开放周期内、或向量版本已过期的块不在结果里——引用它们的一方
    应当把这当作「出处不存在」。
    """
    if not chunk_ids:
        return {}
    params = {
        "chunk_ids": list(chunk_ids),
        "version": _schema_version(),
        "cycle_ids": list(cycle_ids),
    }
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT {_CHUNK_COLUMNS}
            FROM kb_chunks c JOIN kb_source s USING (source_id)
            WHERE c.chunk_id = ANY(%(chunk_ids)s::text[]) AND {_VISIBLE_CHUNK}
            """,
            params,
        ).fetchall()
    return {r["chunk_id"]: _chunk_record(r) for r in rows}


def list_source_chunks(source_id: str, *, cycle_ids: Sequence[int] = ()) -> list[ChunkRecord]:
    """一条条目的全部块,按块序号;条目不存在或当前检索不到(条件同 get_chunks)返回空表。"""
    params = {"source_id": source_id, "version": _schema_version(), "cycle_ids": list(cycle_ids)}
    with _conn() as conn:
        rows = conn.execute(
            f"""
            SELECT {_CHUNK_COLUMNS}
            FROM kb_chunks c JOIN kb_source s USING (source_id)
            WHERE c.source_id = %(source_id)s AND {_VISIBLE_CHUNK}
            ORDER BY c.chunk_ordinal
            """,
            params,
        ).fetchall()
    return [_chunk_record(r) for r in rows]


def list_faq_questions() -> list[tuple[str, str]]:
    """全部可检索 FAQ 的问法,(source_id, 问法) 逐条列出,主问法在前、附加问法在后。"""
    _schema_version()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT f.source_id, f.question, f.alt_questions
            FROM kb_faq f JOIN kb_source s USING (source_id)
            WHERE s.enabled AND s.kind = 'normal'
            ORDER BY f.source_id
            """
        ).fetchall()
    return [
        (r["source_id"], question)
        for r in rows
        for question in [r["question"], *(r["alt_questions"] or [])]
    ]


async def add_alt_question(
    source_id: str, question: str, *, updated_by: str, embedder: Embedder | None = None
) -> bool:
    """给一条 FAQ 加一种问法并重建它的块。已有同样的问法时什么都不做,返回 False。

    条目不存在或不是 FAQ 抛 KbValidationError。重复调用是幂等的。
    """
    record = await asyncio.to_thread(get_source, source_id)
    if record is None or record.type != "faq":
        raise KbValidationError(f"条目 {source_id} 不存在或不是 FAQ,无法添加问法")
    text = question.strip()
    if not text:
        raise KbValidationError("问法不能为空")
    if any(same_question(text, known) for known in [record.question, *record.alt_questions]):
        return False
    await ingest_source(
        SourceInput(
            title=record.title,
            type=record.type,
            kind=record.kind,
            tags=tuple(record.tags),
            question=record.question,
            answer=record.answer,
            updated_by=updated_by,
            cycle_id=record.cycle_id,
            alt_questions=(*record.alt_questions, text),
        ),
        embedder=embedder,
        source_id=source_id,
    )
    return True


def _chunk_record(row: dict[str, Any]) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=row["chunk_id"],
        source_id=row["source_id"],
        title=row["source_title"],
        source_type=row["source_type"],
        text=row["chunk_text"],
        heading_path=row["heading_path"],
        cycle_id=row.get("cycle_id"),
    )


def _fetch_content(conn: psycopg.Connection[dict[str, Any]], source_id: str) -> dict[str, Any]:
    faq = conn.execute(
        "SELECT question, answer, alt_questions FROM kb_faq WHERE source_id = %s", (source_id,)
    ).fetchone()
    if faq:
        return {
            "question": faq["question"],
            "answer": faq["answer"],
            "content_md": "",
            "alt_questions": list(faq.get("alt_questions") or []),
        }
    doc = conn.execute(
        "SELECT content_md FROM kb_doc WHERE source_id = %s", (source_id,)
    ).fetchone()
    if doc:
        return {"question": "", "answer": "", "content_md": doc["content_md"]}
    return {"question": "", "answer": "", "content_md": ""}


def _record(
    row: dict[str, Any], content: dict[str, Any], *, chunk_count: int = 0
) -> SourceRecord:
    return SourceRecord(
        source_id=row["source_id"],
        title=row["source_title"],
        type=row["source_type"],
        kind=row["kind"],
        tags=list(row["tags"] or []),
        enabled=row["enabled"],
        updated_by=row["updated_by"],
        question=content.get("question", ""),
        answer=content.get("answer", ""),
        content_md=content.get("content_md", ""),
        chunk_count=chunk_count,
        cycle_id=row.get("cycle_id"),
        alt_questions=list(content.get("alt_questions") or []),
    )


def _to_pgvector(vector: list[float]) -> str:
    """pgvector 文本入参:'[0.1,0.2,...]'(psycopg 无原生 vector adapter)。"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
