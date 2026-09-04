"""后端 API 客户端(TOOL-01):服务账号登录、token 续期、统一错误映射、瞬时错误重试。

契约(对齐后端 openapi.yaml 与 BusinessExceptionEnum):
- 登录 POST /api/auth/login(body: auth_id/auth_type/verify,HTTP 201),
  返回 {code, message, data:{token, user_id, roleNames}};业务码 200=成功。
- 业务错误在 body.code,HTTP 状态随枚举漂移(400/401/403/409...),
  故判定一律以 body.code 为准,HTTP 401 例外(Jwt 过滤器直接 setStatus,无 body)。
- token 类失效码 {1001,1002,1003,2004,2006} 或 HTTP 401 → 重登一次后重试。
- 登录失败实测返回 code 401(枚举写作 2002,以后端为准,2026-08-31 冒烟验证)。
"""

import asyncio
import logging
from typing import Any, cast

import httpx

from official_agent import credentials
from official_agent.config import Settings, get_settings

logger = logging.getLogger(__name__)

_SUCCESS_CODE = 200
_LOGIN_PATH = "/api/auth/login"

# 登录态失效的业务码(BusinessExceptionEnum:JWT 1001-1003 / 用户 2004,2006)
_AUTH_EXPIRED_CODES = frozenset({1001, 1002, 1003, 2004, 2006})

# 高频业务码 → 可行动提示;未列出的码透传后端 message
_ACTIONABLE_HINTS: dict[int, str] = {
    2002: "服务账号用户名或密码错误,检查 .env 的 BACKEND_SERVICE_* 配置",
    2101: "服务账号权限不足,需后端补授该操作的权限(SEC-01 谈判项)",
    3001: "简历不存在,先用 search_resumes 核对 resume_id/user_id 与周期",
    3010: "该周期已停止投递,不可再修改",
    3407: "候选人尚未提交简历,不能进入预约/评分流程",
    3604: "目标场次已满,用 find_available_sessions 查其他场次剩余容量",
    3606: "该场次已有面试安排,不能删除",
    4291: "请求过于频繁,等待数秒后重试",
}

# 瞬时错误重试:仅幂等的 GET 自动重试;写操作不重试(非幂等,重试可能双写)
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (0.2, 0.5)


class BackendError(Exception):
    """后端调用失败。message 必须对模型可行动:说清哪里错、该怎么改。"""


class BackendAuthError(BackendError):
    """登录本身失败(凭证错误/账号异常)。重登解决不了,需人工检查配置。"""


class _AuthExpired(Exception):
    """内部信号:登录态失效,调用方应重登。不对外暴露。"""


class BackendClient:
    """httpx 异步客户端,持有服务账号 JWT。

    线程/协程安全:token 惰性获取与重登共用一把锁,并发首调只登录一次。
    """

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._http = http or httpx.AsyncClient(
            base_url=self._settings.backend_base_url, timeout=15.0
        )
        self._token: str | None = None
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        # headers 用于附加代理身份等自定义头;Authorization 由本层注入,不可覆盖
        return await self._request("GET", path, params=params, headers=headers)

    @property
    def token(self) -> str | None:
        """当前缓存的服务/会话 token(只读)。CLI 等入口层据此把登录
        令牌传给工具装配(candidate 的 get_my_interview 绑定)。"""
        return self._token

    async def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, json=json)

    async def put(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return await self._request("PUT", path, json=json)

    async def get_as_user(
        self, path: str, params: dict[str, Any] | None = None, user_token: str = ""
    ) -> Any:
        """以最终用户本人令牌裸发请求(get_my_interview 等场景)。

        与服务账号通道语义不同,故不做重登与重试:用户 token 无效/过期
        是另一类失败,如实抛 BackendError 由调用方引导用户重新登录,
        绝不能拿服务账号悄悄顶替。
        出现第三个用户令牌工具时应拆出 UserTokenClient(review #73,SMELL)。
        """
        if not user_token:
            raise BackendError("缺少用户本人令牌(user_token),该操作必须以最终用户身份执行")
        try:
            resp = await self._http.request(
                "GET", path, params=params, headers={"Authorization": f"Bearer {user_token}"}
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # 用户令牌通道不自动重试(非幂等语义不明确),只映射成可行动文案
            raise BackendError(
                f"后端连接失败({type(exc).__name__}),稍后重试;持续失败请检查后端状态"
            ) from None
        try:
            return _interpret(resp)
        except _AuthExpired:
            # body 业务码过期(1001-1003/2004/2006)与 HTTP 401 同文案,不泄漏内部信号
            raise BackendError("用户令牌无效或已过期,需用户重新登录后重试") from None

    async def login(self) -> str:
        """服务账号登录并缓存 token。凭证错误抛 BackendAuthError。"""
        token = await self._do_login()
        self._token = token
        return token

    async def _ensure_token(self) -> str:
        """token 获取优先级(SEC-09):
        内存缓存 → 本地存储凭证(official-agent login 的产物)→ 账密 login。
        .env 无账密且无存储凭证时,报可行动指引(运行 official-agent login)。
        """
        if self._token:
            return self._token
        stored = credentials.load()
        if stored is not None:
            self._token = cast("str", stored["token"])
            return self._token
        async with self._login_lock:
            if self._token is None:
                if not self._settings.backend_service_username:
                    raise BackendAuthError(
                        "未配置凭证:请先运行 official-agent login,或在 .env 设置 "
                        "BACKEND_SERVICE_USERNAME/PASSWORD"
                    )
                self._token = await self._do_login()
            return self._token

    async def _do_login(self) -> str:
        if not self._settings.backend_service_username:
            raise BackendAuthError(
                "未配置服务账号凭证,请在 .env 设置 BACKEND_SERVICE_USERNAME/PASSWORD"
            )
        resp = await self._http.post(
            _LOGIN_PATH,
            json={
                "auth_id": self._settings.backend_service_username,
                "auth_type": "username-password",
                "verify": self._settings.backend_service_password,
            },
        )
        body = _parse_json(resp)
        if body.get("code") != _SUCCESS_CODE:
            raise BackendAuthError(_error_message(body))
        token = (body.get("data") or {}).get("token")
        if not token:
            raise BackendError("登录响应缺少 token 字段,后端契约可能已变更")
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        token = await self._ensure_token()
        try:
            return await self._send(
                method, path, params=params, json=json, token=token, headers=headers
            )
        except _AuthExpired:
            # 重登一次后重试;再失效说明账号/服务端有真问题,如实抛出。
            # 锁内先看 token 是否已被并发协程换新,避免重复登录。
            async with self._login_lock:
                if self._token is None or self._token == token:
                    self._token = await self._do_login()
                token = self._token
            try:
                return await self._send(
                    method, path, params=params, json=json, token=token, headers=headers
                )
            except _AuthExpired:
                raise BackendError(
                    "重新登录后请求仍被拒,服务账号可能被禁用或后端鉴权异常"
                ) from None

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
        token: str,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """发送单轮请求:瞬时错误重试(GET)+ 统一错误解释。"""
        attempts = _MAX_ATTEMPTS if method == "GET" else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                merged_headers = {"Authorization": f"Bearer {token}"}
                if headers:
                    merged_headers.update(
                        {k: v for k, v in headers.items() if k.lower() != "authorization"}
                    )
                resp = await self._http.request(
                    method, path, params=params, json=json, headers=merged_headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    logger.warning(
                        "backend %s %s 瞬时错误(%s),第 %d/%d 次重试前等待 %.1fs",
                        method, path, type(exc).__name__, attempt + 2, attempts, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                break
            return _interpret(resp)
        raise BackendError(
            f"后端连接失败({type(last_exc).__name__}: {last_exc}),"
            + ("已自动重试仍失败" if attempts > 1 else "写操作不自动重试,确认后端状态后可重发")
        ) from last_exc


def _parse_json(resp: httpx.Response) -> dict[str, Any]:
    """容错解析:网关错误(502/504)常返回 HTML,此时状态码比 body 更有用。"""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        return body
    raise BackendError(
        f"后端返回非 JSON(HTTP {resp.status_code}),服务可能不可用或网关异常"
    )


def _interpret(resp: httpx.Response) -> Any:
    """把响应翻译成 data / 异常。业务码优先于 HTTP 状态码。"""
    # Jwt 过滤器失效路径:HTTP 401 直接 setStatus,可能无 body
    if resp.status_code == 401:
        raise _AuthExpired()
    body = _parse_json(resp)
    code = body.get("code")
    if code == _SUCCESS_CODE:
        return body.get("data")
    if code in _AUTH_EXPIRED_CODES:
        raise _AuthExpired()
    raise BackendError(_error_message(body))


def _error_message(body: dict[str, Any]) -> str:
    """业务错误 → 可行动文案:后端 message + code + 处置提示。"""
    code = body.get("code")
    message = body.get("message", "未知错误")
    hint = _ACTIONABLE_HINTS.get(code) if code is not None else None
    parts = [f"{message}(code {code})"]
    if hint:
        parts.append(hint)
    return "。".join(parts)
