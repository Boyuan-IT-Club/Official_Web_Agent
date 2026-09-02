"""auth_status 只读工具(SEC-09):让模型能答「我是谁、凭证还能用多久」。

红线:输出**不含 token 本身**——只有身份字段与剩余时长。已过期时
返回 expired=True 并提示重新 login(可行动)。
"""

import time
from typing import Any

from boyuan_agent import credentials


def auth_status() -> dict[str, Any]:
    """查询当前后端凭证状态:是否有效、以谁的身份、剩余时长。

    本工具不触碰任何数据查询,仅报告凭证;token 本身不会出现在结果中。
    过期时返回可行动提示(重新运行 boyuan-agent login)。
    """
    raw = credentials.load_raw()
    if raw is None:
        return {
            "authenticated": False,
            "expired": False,
            "hint": "尚未登录:请在本机运行 boyuan-agent login(勿把密码发给模型)",
        }
    remaining = max(0, int(raw.get("exp", 0)) - int(time.time()))
    expired = remaining <= 0
    status: dict[str, Any] = {
        "authenticated": not expired,
        "expired": expired,
        "user_id": raw.get("user_id"),
        "username": raw.get("username", ""),
        "remaining_minutes": remaining // 60,
    }
    if expired:
        status["hint"] = "凭证已过期:请重新运行 boyuan-agent login"
    return status
