"""后端 API 客户端(TOOL-01):服务账号登录、token 续期、统一错误映射。"""

from typing import Any

import httpx

from boyuan_agent.config import get_settings


class BackendError(Exception):
    """后端调用失败。message 必须对模型可行动:说清哪里错、该怎么改。"""


class BackendClient:
    """httpx 异步客户端,持有服务账号 JWT。

    TODO(TOOL-01):
    - login() 走 POST /api/auth/login,缓存 token,401 时自动重登一次
    - 后端 ResponseMessage 的业务错误码 → 可行动的 BackendError 文案
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._http = httpx.AsyncClient(base_url=settings.backend_base_url, timeout=15.0)
        self._token: str | None = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError("TOOL-01")

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        raise NotImplementedError("TOOL-01")
