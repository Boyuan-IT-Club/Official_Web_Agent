"""KB 表自举(RAG #134):幂等 DDL 进仓库,新环境可自举(L-1 先例)。

- CREATE EXTENSION vector 需超级用户(容器内 postgres 即是);镜像不带
  pgvector 时给出可操作的报错(本地 compose 已换 pgvector/pgvector:pg17)
- 内容模型(#118):kb_source 共享来源层;kb_faq/kb_doc 两张内容表;
  kb_chunks 向量块;kb_meta 单行记 embed_model/dim/version
- 禁跨模型向量混排(#119):meta 与配置的 model/dim 不一致 → 清空
  chunks + 版本 bump + 列维度 ALTER,等调用方全量 reindex
"""

from typing import Any

import psycopg

from official_agent.config import get_settings


class KbSchemaError(RuntimeError):
    """pgvector 扩展不可用等 schema 层失败。"""


def current_embed_target() -> tuple[str, int]:
    """配置里的 (embed_model, dim)。dim<=0 视为未配置维度,拒绝建表。"""
    settings = get_settings()
    if not settings.embed_model or settings.embed_dim <= 0:
        raise RuntimeError("EMBED_MODEL/EMBED_DIM 未配置,无法确定向量表维度")
    return settings.embed_model, settings.embed_dim


def ensure_kb_schema(
    conn: psycopg.Connection[dict[str, Any]], *, embed_model: str, dim: int
) -> int:
    """建齐 KB 表,返回当前生效的 meta.version。调用方管理事务。

    首次:按配置建 meta(version=1)与对应维度的 chunks 表。
    换模型/换维度:清空 chunks、meta.version+1、embedding 列 ALTER 到新维度
    ——此后检索按 version 过滤自然拿不到旧向量,直到逐条 reindex。
    """
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except psycopg.Error as exc:
        raise KbSchemaError(
            "pgvector 扩展不可用(CREATE EXTENSION vector 失败):"
            "请用 pgvector/pgvector 镜像(deploy/docker-compose.local.yml 已配置)"
            f"或在该库安装扩展;原始错误:{exc}"
        ) from exc

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_source (
            source_id    text        NOT NULL PRIMARY KEY,
            source_title text        NOT NULL,
            source_type  text        NOT NULL CHECK (source_type IN ('faq', 'doc')),
            kind         text        NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal', 'test')),
            tags         text[]      NOT NULL DEFAULT '{}',
            enabled      boolean     NOT NULL DEFAULT true,
            updated_by   text        NOT NULL DEFAULT '',
            created_at   timestamptz NOT NULL DEFAULT now(),
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_faq (
            source_id text NOT NULL PRIMARY KEY REFERENCES kb_source(source_id) ON DELETE CASCADE,
            question  text NOT NULL,
            answer    text NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_doc (
            source_id  text NOT NULL PRIMARY KEY REFERENCES kb_source(source_id) ON DELETE CASCADE,
            content_md text NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_meta (
            id          integer     NOT NULL PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            embed_model text        NOT NULL,
            dim         integer     NOT NULL,
            version     integer     NOT NULL DEFAULT 1,
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    dim = int(dim)

    def create_chunks_table() -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS kb_chunks (
                chunk_id      text        NOT NULL PRIMARY KEY,
                source_id     text        NOT NULL REFERENCES kb_source(source_id)
                                          ON DELETE CASCADE,
                chunk_ordinal integer     NOT NULL,
                chunk_text    text        NOT NULL,
                heading_path  text        NOT NULL DEFAULT '',
                embedding     vector({dim}) NOT NULL,
                model_version integer     NOT NULL,
                created_at    timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        # HNSW cosine(#119 起步参数默认);向量列为空表时建索引瞬时完成
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_chunks_hnsw "
            "ON kb_chunks USING hnsw (embedding vector_cosine_ops)"
        )

    row = conn.execute(
        "SELECT embed_model, dim, version FROM kb_meta WHERE id = 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO kb_meta (id, embed_model, dim, version) VALUES (1, %s, %s, 1)",
            (embed_model, dim),
        )
        create_chunks_table()
        return 1

    if row["embed_model"] == embed_model and row["dim"] == dim:
        create_chunks_table()  # 表可能尚未建(只建了 meta 的极端路径)
        return row["version"]

    # 换模型/维度:旧向量整体失效,绝不与新模型向量混排。
    # 先补齐表(极端路径 meta 在而表不在),再清数据;ALTER TYPE 会自动重建
    # 依赖索引,无需手动 DROP/CREATE。
    create_chunks_table()
    conn.execute("DELETE FROM kb_chunks")
    conn.execute(f"ALTER TABLE kb_chunks ALTER COLUMN embedding TYPE vector({dim})")
    new_version = row["version"] + 1
    conn.execute(
        "UPDATE kb_meta SET embed_model = %s, dim = %s, version = %s, updated_at = now() "
        "WHERE id = 1",
        (embed_model, dim, new_version),
    )
    return new_version
