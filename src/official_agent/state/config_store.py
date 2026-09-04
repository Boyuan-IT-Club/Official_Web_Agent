"""agent_config 键值表(M6 #111):管理员可热载的低敏配置存 DB。

决策(#100/#101/#109):
- 两级配置:低敏可入库热载(model_strong/model_light/llm_provider/llm_base_url),
  高敏留 .env(真实 API key/backend 密码/postgres_url/host/port)——真实凭证永不入库。
- 表建在 agent 自有 PG(与 conversation_log/audit 同库);Backend 不做。
- 改动热生效:get_settings 去缓存 + 重建依赖该配置的 LLM client。

与 agent_audit_log / agent_threads 同库,同 state/* 先例。
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from official_agent.config import get_settings


def _conn() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().postgres_url, row_factory=dict_row)


def ensure_config_table() -> None:
    """幂等建 agent_config 表(#111;同 conversation/audit L-1 自举)。"""
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_config (
                key        text PRIMARY KEY,
                value      text NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now()
            );
            """
        )


def get_all_config() -> dict[str, str]:
    """读全部配置键值(供管理 API 回显 / 热生效合并)。"""
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM agent_config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_config(key: str, value: str) -> None:
    """upsert 一个配置键(仅低敏白名单键由调用方保证)。"""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_config (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                updated_at = now()
            """,
            (key, value),
        )


def delete_config(key: str) -> None:
    """删除一个配置键(恢复 env 默认)。"""
    with _conn() as conn:
        conn.execute("DELETE FROM agent_config WHERE key = %s", (key,))
