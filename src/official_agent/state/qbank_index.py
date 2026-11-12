"""题目向量索引的数据面:一道可挑的题一行,供跨简历的通用题检测使用。

- 与题库两表(state/qbank.py)同库,但自举分开且惰性:本表依赖 pgvector 扩展,
  不能让「库里没装扩展」连带挡住题库的自举
- 向量表纪律(与知识库同规则,ADR-0007):每行记 embed_model,比较只在同一个
  模型的行之间做;换模型后旧行不再参与比较,等对应简历重跑出题时被整批替换
- 一份简历在一个周期里只留最新一版题库的行;比较只在同一个周期内做
- 题文入库前由调用方脱敏;本模块不碰简历原文
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings

_bootstrap_lock = threading.Lock()
_bootstrapped = False


@dataclass(frozen=True)
class IndexRow:
    """索引里的一行:稳定题引用 id、题目的出处与角色、脱敏后的题文、向量。"""

    ref_id: str
    group_kind: str
    role: str
    category: str
    question: str
    embedding: list[float]


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def _to_pgvector(vector: list[float]) -> str:
    """pgvector 文本入参:'[0.1,0.2,...]'(psycopg 没有原生的 vector 适配)。"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _ensure_bootstrapped() -> None:
    """建表每进程只做一次:DDL 即使是 no-op 也要拿表锁,不能放在数据路径上反复跑。"""
    global _bootstrapped
    if _bootstrapped:
        return
    with _bootstrap_lock:
        if _bootstrapped:
            return
        with _conn() as conn:
            ensure_qbank_index_table(conn)
        _bootstrapped = True


def ensure_qbank_index_table(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """建索引表(幂等)。库里装不了 pgvector 时抛 psycopg.Error,调用方管理事务。

    embedding 列不带维度:维度由 embed_model 决定,而同一张表里可以并存换模型
    前后的行。代价是建不了向量索引——一个周期的题目是几千行的量级,顺序扫够用。
    """
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qbank_question_index (
            ref_id        text        NOT NULL PRIMARY KEY,
            resume_id     bigint      NOT NULL,
            cycle_id      int         NOT NULL,
            qbank_version int         NOT NULL,
            group_kind    text        NOT NULL,
            role          text        NOT NULL,
            category      text        NOT NULL DEFAULT '',
            question      text        NOT NULL,
            embedding     vector      NOT NULL,
            embed_model   text        NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qbank_question_index_cycle "
        "ON qbank_question_index (cycle_id, resume_id)"
    )


def replace_resume_rows(
    *,
    resume_id: int,
    cycle_id: int,
    qbank_version: int,
    embed_model: str,
    rows: list[IndexRow],
) -> None:
    """把一份简历在本周期的索引行整批换成这一版题库的(单事务)。

    旧版题库的行一并删掉:它们的题引用 id 已经随版本作废,留着会让这份简历
    已经不再出的题继续参与别人的通用题检测。
    """
    _ensure_bootstrapped()
    with _conn() as conn:
        conn.execute(
            "DELETE FROM qbank_question_index WHERE resume_id = %s AND cycle_id = %s",
            (resume_id, cycle_id),
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO qbank_question_index
                    (ref_id, resume_id, cycle_id, qbank_version, group_kind, role,
                     category, question, embedding, embed_model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                """,
                (
                    row.ref_id,
                    resume_id,
                    cycle_id,
                    qbank_version,
                    row.group_kind,
                    row.role,
                    row.category,
                    row.question,
                    _to_pgvector(row.embedding),
                    embed_model,
                ),
            )


def count_similar_resumes(
    *,
    resume_id: int,
    cycle_id: int,
    embed_model: str,
    embeddings: list[list[float]],
    min_similarity: float,
) -> list[int]:
    """每个向量在本周期**其他简历**里有几份简历出过近似的题;返回与入参同序的计数。

    近似 = 余弦相似度不低于 min_similarity。按简历去重计数:同一份简历里有三道
    近似题也只算一份。
    """
    if not embeddings:
        return []
    _ensure_bootstrapped()
    # pgvector 的 <=> 是余弦距离(1 - 相似度)。两个向量维度不同会直接报错,所以
    # 模型过滤写在 CASE 里:WHERE 的各个条件谁先算没有保证,CASE 的分支有。
    max_distance = 1.0 - min_similarity
    counts: list[int] = []
    with _conn() as conn:
        for embedding in embeddings:
            row = conn.execute(
                """
                SELECT count(DISTINCT resume_id) AS resumes
                FROM qbank_question_index
                WHERE cycle_id = %s
                  AND resume_id <> %s
                  AND CASE WHEN embed_model = %s
                           THEN (embedding <=> %s::vector) <= %s
                           ELSE false END
                """,
                (cycle_id, resume_id, embed_model, _to_pgvector(embedding), max_distance),
            ).fetchone()
            counts.append(int(row["resumes"]) if row else 0)
    return counts
