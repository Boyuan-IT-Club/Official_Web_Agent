"""运营配置管理面:/admin/config——低敏配置热改 + 高敏凭证掩码回显。

从 routes.py 拆出(原「配置管理」与「用户聊天」同居一文件):配置白名单
校验、secret 掩码、llm_base_url 安全校验是领域规则,与 SSE 会话路由无关。
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from official_agent.graphs.identity import ResolvedIdentity
from official_agent.web.auth import require_any

router = APIRouter()

# 管理 API 认证:官网 JWT → resolve → permission_codes 含 agent:monitor
_require_monitor = require_any("agent:monitor")

# 高敏键(Settings 字段名):真实凭证,只读回显掩码,永不入库/不可在线改。
_SECRET_SETTINGS_FIELDS: frozenset[str] = frozenset(
    {
        "backend_service_username",
        "backend_service_password",
        "llm_api_key",
        "anthropic_api_key",
        "postgres_url",
        "feishu_app_id",
        "feishu_app_secret",
        "feishu_verification_token",
        "feishu_encrypt_key",
        "langfuse_public_key",
        "langfuse_secret_key",
    }
)


def _mask_secret(value: str) -> str:
    """掩码末 4 位(短值整掩)。"""
    return value[-4:] if len(value) >= 4 else "****"


@router.get("/admin/config")
async def get_admin_config(
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """回显配置:低敏键实值(DB 覆盖优先) + 高敏键掩码({configured, masked})。"""
    from official_agent.config import HOT_KEYS, get_settings

    settings = get_settings()
    try:
        db_overrides = await asyncio.to_thread(get_all_config)
    except Exception:  # noqa: BLE001 — PG 不可用 → 只显示 env(fail-open)
        db_overrides = {}

    result: dict[str, Any] = {}
    for field in HOT_KEYS:
        result[field] = db_overrides.get(field, getattr(settings, field, ""))
    for field in sorted(_SECRET_SETTINGS_FIELDS):
        value = getattr(settings, field, "") or ""
        result[field] = {
            "configured": bool(value),
            "masked": _mask_secret(value) if value else "",
        }
    return result


# 安全:llm_base_url 若被改成任意端点,下轮重建 agent 时
# .env 的 LLM key 会作为 Bearer 发往该端点 → key 泄漏。只允许 https + 受信 host。
_ALLOWED_LLM_HOSTS: tuple[str, ...] = (
    "api.deepseek.com",
    "api.openai.com",
    "api.anthropic.com",
    "open.bigmodel.cn",
)


def _validate_base_url(value: str) -> None:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(status_code=400, detail="llm_base_url 必须为 https 且含 host")
    if parsed.hostname not in _ALLOWED_LLM_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=f"llm_base_url 的 host 不在白名单: {parsed.hostname}",
        )


@router.put("/admin/config")
async def put_admin_config(
    body: dict[str, str],
    _: Annotated[ResolvedIdentity, Depends(_require_monitor)],
) -> dict[str, Any]:
    """改低敏键(HOT_KEYS 白名单)并热生效;非白名单(高敏)→ 400。"""
    from official_agent.config import HOT_KEYS

    invalid_keys = [k for k in body if k not in HOT_KEYS]
    if invalid_keys:
        raise HTTPException(status_code=400, detail=f"不可热载的键: {', '.join(invalid_keys)}")
    for key, value in body.items():
        value = (value or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"{key} 值不能为空")
        if key == "llm_base_url":
            _validate_base_url(value)
        await asyncio.to_thread(set_config, key, value)
    # 全部键成功写库后才失效缓存(避免部分写 + 缓存未刷的分离)
    invalidate_settings_cache()
    return {"updated": list(body.keys())}


def get_all_config() -> dict[str, str]:
    """读 agent_config 全部键值(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.config_store import get_all_config as _impl

    return _impl()


def set_config(key: str, value: str) -> None:
    """upsert 一个配置键(lazy;模块级包装供测试 patch)。"""
    from official_agent.state.config_store import set_config as _impl

    _impl(key, value)


def invalidate_settings_cache() -> None:
    """使 get_settings 缓存失效(lazy;模块级包装供测试 patch)。"""
    from official_agent.config import invalidate_settings_cache as _impl

    _impl()
