"""凭证保管(SEC-09):login 换取的 token 存本地,生命周期感知。

存储:~/.boyuan-agent/credentials.json,mode 600,内容
    {"token": "...", "exp": 1700000000, "user_id": 1, "username": "..."}

红线(ADR-0006):token 只在本文件与 client 内存;**不进模型可见面、
不进图 state/checkpointer**。auth_status 工具只回「剩余时长/身份」,
永不回 token 本身。

升级路径(刻意不做):OS keychain 加密、refresh_token 滚动、PAT 签发
端点(官网设置页)——存储格式为 PAT 预留(scope 字段)。
"""

import json
import time
from pathlib import Path
from typing import Any

_CREDENTIALS_DIR = Path.home() / ".boyuan-agent"
_CREDENTIALS_FILE = _CREDENTIALS_DIR / "credentials.json"


def save(token: str, exp: int, user_id: int | None = None, username: str = "") -> None:
    """保存 token 与过期时间(epoch 秒)。文件权限 600。"""
    _CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"token": token, "exp": exp, "user_id": user_id, "username": username}
    path = _CREDENTIALS_FILE
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    path.chmod(0o600)


def load() -> dict[str, Any] | None:
    """读取有效(未过期)的存储凭证;无文件/过期/损坏返回 None。

    过期即视为不存在——调用方无需区分「没有」与「过期」。
    """
    raw = load_raw()
    if raw is None:
        return None
    if raw.get("exp", 0) <= time.time():
        return None
    return raw


def load_raw() -> dict[str, Any] | None:
    """含过期凭证的原始读取(过期提示需要知道 exp 时刻)。"""
    try:
        raw = json.loads(_CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("token") else None


def clear() -> None:
    """清除存储凭证(过期清理/登出)。"""
    _CREDENTIALS_FILE.unlink(missing_ok=True)


def remaining_seconds() -> int | None:
    """token 剩余秒数;无有效凭证返回 None。"""
    raw = load()
    if raw is None:
        return None
    return max(0, int(raw.get("exp", 0)) - int(time.time()))
