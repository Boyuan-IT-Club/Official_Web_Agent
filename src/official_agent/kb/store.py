"""KB 数据面(RAG #134 R1):知识条目 CRUD 与向量检索。

- SQL 同步(threads.py 先例:mock 解耦单测,真库往返走集成档);
  异步入口用 asyncio.to_thread 包装,不阻塞事件循环
- 入库单事务原子:source + 内容表 + chunks 同生共死
- kind=test 条目可入库但不进生产检索(search 默认排除;eval 显式 include_test)
- 检索只回 enabled 且 model_version=当前 meta.version 的块(禁混排的读侧配套)
"""

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings
from official_agent.kb.chunking import KbChunk, embed_text, split_doc, split_faq
from official_agent.kb.embedding import Embedder, embed_texts
from official_agent.kb.schema import current_embed_target, ensure_kb_schema

_SOURCE_COLUMNS = (
    "source_id, source_title, source_type, kind, tags, enabled, updated_by, created_at, updated_at"
)


class KbValidationError(ValueError):
    """条目内容不满足入库要求(R2 映射 400)。"""


@dataclass(frozen=True)
class SourceInput:
    """新建/更新一条知识的输入。type=faq 用 question/answer;type=doc 用 content_md。"""

    title: str
    type: str  # 'faq' | 'doc'
    kind: str = "normal"  # 'normal' | 'test'
    tags: tuple[str, ...] = ()
    question: str = ""
    answer: str = ""
    content_md: str = ""
    updated_by: str = ""


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


@dataclass(frozen=True)
class SearchHit:
    """一条检索命中。source_id+title 是引用锚;chunk_text 供上下文/摘要。"""

    source_id: str
    title: str
    chunk_text: str
    heading_path: str
    score: float


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def new_source_id() -> str:
    return f"kb_{secrets.token_hex(6)}"


def _split_chunks(source: SourceInput) -> list[KbChunk]:
    if source.kind not in ("normal", "test"):
        raise KbValidationError(f"kind 只支持 normal/test,收到 {source.kind!r}")
    if source.type == "faq":
        if not source.question.strip() or not source.answer.strip():
            raise KbValidationError("FAQ 条目必须同时有 question 与 answer")
        return split_faq(source.question, source.answer)
    if source.type == "doc":
        if not source.content_md.strip():
            raise KbValidationError("正文条目 content_md 不能为空")
        return split_doc(source.content_md)
    raise KbValidationError(f"source_type 只支持 faq/doc,收到 {source.type!r}")


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
    model, dim = current_embed_target()
    sid = source_id or new_source_id()
    with _conn() as conn:
        version = ensure_kb_schema(conn, embed_model=model, dim=dim)
        conn.execute(
            """
            INSERT INTO kb_source (source_id, source_title, source_type, kind, tags, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                source_title = EXCLUDED.source_title,
                source_type = EXCLUDED.source_type,
                kind = EXCLUDED.kind,
                tags = EXCLUDED.tags,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (sid, source.title, source.type, source.kind, list(source.tags), source.updated_by),
        )
        # 内容两表都清再插:类型迁移(faq↔doc)天然正确
        conn.execute("DELETE FROM kb_faq WHERE source_id = %s", (sid,))
        conn.execute("DELETE FROM kb_doc WHERE source_id = %s", (sid,))
        if source.type == "faq":
            conn.execute(
                "INSERT INTO kb_faq (source_id, question, answer) VALUES (%s, %s, %s)",
                (sid, source.question, source.answer),
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
    empty_content = {"question": "", "answer": "", "content_md": ""}
    items = [
        _record(r, empty_content, chunk_count=r["chunk_count"]) for r in rows
    ]
    return {"items": items, "total": total["n"] if total else 0, "page": page, "size": size}


async def search(
    query: str, *, top_k: int = 4, include_test: bool = False, embedder: Embedder | None = None
) -> list[SearchHit]:
    """向量检索 top-k。默认排除 kind=test;0 命中返回空表(降级话术归调用方)。"""
    do_embed = embedder or embed_texts
    vectors = await do_embed([query])
    query_vec = vectors[0]
    return await asyncio.to_thread(_search_sync, query_vec, top_k, include_test)


def _search_sync(query_vec: list[float], top_k: int, include_test: bool) -> list[SearchHit]:
    model, dim = current_embed_target()
    with _conn() as conn:
        # 同事务内先自举再查:DDL 可见,且省一次连接建立
        version = ensure_kb_schema(conn, embed_model=model, dim=dim)
        kind_filter = "" if include_test else " AND s.kind = 'normal'"
        rows = conn.execute(
            f"""
            SELECT s.source_id, s.source_title, c.chunk_text, c.heading_path,
                   1 - (c.embedding <=> %s::vector) AS score
            FROM kb_chunks c JOIN kb_source s USING (source_id)
            WHERE s.enabled AND c.model_version = %s{kind_filter}
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (_to_pgvector(query_vec), version, _to_pgvector(query_vec), top_k),
        ).fetchall()
    return [
        SearchHit(
            source_id=r["source_id"],
            title=r["source_title"],
            chunk_text=r["chunk_text"],
            heading_path=r["heading_path"],
            score=float(r["score"]),
        )
        for r in rows
    ]


def _fetch_content(conn: psycopg.Connection[dict[str, Any]], source_id: str) -> dict[str, str]:
    faq = conn.execute(
        "SELECT question, answer FROM kb_faq WHERE source_id = %s", (source_id,)
    ).fetchone()
    if faq:
        return {"question": faq["question"], "answer": faq["answer"], "content_md": ""}
    doc = conn.execute(
        "SELECT content_md FROM kb_doc WHERE source_id = %s", (source_id,)
    ).fetchone()
    if doc:
        return {"question": "", "answer": "", "content_md": doc["content_md"]}
    return {"question": "", "answer": "", "content_md": ""}


def _record(
    row: dict[str, Any], content: dict[str, str], *, chunk_count: int = 0
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
    )


def _to_pgvector(vector: list[float]) -> str:
    """pgvector 文本入参:'[0.1,0.2,...]'(psycopg 无原生 vector adapter)。"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
