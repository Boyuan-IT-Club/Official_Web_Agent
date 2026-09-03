"""TOOL-01 客户端单测:登录/续期/错误映射/重试,全部走 respx 模拟,不碰真后端。"""

import asyncio

import httpx
import pytest
import respx

from official_agent.config import Settings
from official_agent.tools.client import (
    BackendAuthError,
    BackendClient,
    BackendError,
)

BASE = "http://backend.test"
LOGIN = f"{BASE}/api/auth/login"
CYCLES = f"{BASE}/api/cycles/open"


def make_client() -> BackendClient:
    settings = Settings(
        _env_file=None,
        backend_base_url=BASE,
        backend_service_username="svc-agent",
        backend_service_password="secret",
    )
    return BackendClient(http=httpx.AsyncClient(base_url=BASE), settings=settings)


def login_ok(token: str = "tok-1") -> httpx.Response:
    return httpx.Response(
        201, json={"code": 200, "message": "ok", "data": {"token": token, "user_id": 1}}
    )


@respx.mock
async def test_first_request_logs_in_and_carries_bearer() -> None:
    login_route = respx.post(LOGIN)
    login_route.side_effect = login_ok("tok-1")
    api_route = respx.get(CYCLES)
    api_route.side_effect = httpx.Response(
        200, json={"code": 200, "message": "ok", "data": {"cycleId": 2}}
    )
    client = make_client()

    data = await client.get("/api/cycles/open")

    assert data == {"cycleId": 2}
    assert login_route.called
    # 业务请求必须带缓存到的 token
    assert api_route.calls.last.request.headers["Authorization"] == "Bearer tok-1"
    await client.aclose()


@respx.mock
async def test_http_401_triggers_relogin_and_retry() -> None:
    login_route = respx.post(LOGIN)
    login_route.side_effect = [login_ok("tok-old"), login_ok("tok-new")]
    api_route = respx.get(CYCLES)
    api_route.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json={"code": 200, "message": "ok", "data": {"cycleId": 2}}),
    ]
    client = make_client()

    data = await client.get("/api/cycles/open")

    assert data == {"cycleId": 2}
    # 首次登录 + 过期重登 = 2;业务请求 401 一次 + 重试成功一次 = 2
    assert login_route.call_count == 2
    assert api_route.call_count == 2
    assert api_route.calls.last.request.headers["Authorization"] == "Bearer tok-new"
    await client.aclose()


@respx.mock
async def test_auth_expired_business_code_triggers_relogin() -> None:
    """token 过期走业务码 1002(HTTP 409),同样要触发重登。"""
    respx.post(LOGIN).side_effect = [login_ok("tok-a"), login_ok("tok-b")]
    respx.get(CYCLES).side_effect = [
        httpx.Response(409, json={"code": 1002, "message": "token已过期"}),
        httpx.Response(200, json={"code": 200, "message": "ok", "data": None}),
    ]

    client = make_client()
    assert await client.get("/api/cycles/open") is None
    await client.aclose()


@respx.mock
async def test_relogin_twice_fails_with_actionable_error() -> None:
    """重登后仍被拒:不再无限循环,抛可行动错误而非内部信号。"""
    respx.post(LOGIN).side_effect = [login_ok(), login_ok()]
    respx.get(CYCLES).side_effect = [httpx.Response(401), httpx.Response(401)]

    client = make_client()
    with pytest.raises(BackendError, match="重新登录后请求仍被拒"):
        await client.get("/api/cycles/open")
    await client.aclose()


@respx.mock
async def test_bad_credentials_raises_auth_error() -> None:
    respx.post(LOGIN).side_effect = httpx.Response(
        409, json={"code": 2002, "message": "用户名或密码错误"}
    )
    client = make_client()

    with pytest.raises(BackendAuthError, match="BACKEND_SERVICE"):
        await client.get("/api/cycles/open")
    await client.aclose()


@respx.mock
async def test_missing_credentials_fails_fast() -> None:
    settings = Settings(_env_file=None, backend_base_url=BASE)
    client = BackendClient(http=httpx.AsyncClient(base_url=BASE), settings=settings)

    with pytest.raises(BackendAuthError, match="未配置凭证"):
        await client.get("/api/cycles/open")
    await client.aclose()


@respx.mock
async def test_business_error_maps_to_actionable_hint() -> None:
    respx.post(LOGIN).side_effect = login_ok()
    respx.get(CYCLES).side_effect = httpx.Response(
        409, json={"code": 3604, "message": "该面试场次已满"}
    )
    client = make_client()

    with pytest.raises(BackendError) as exc_info:
        await client.get("/api/cycles/open")
    # 后端 message + code + 可行动提示三要素齐全
    assert "该面试场次已满" in str(exc_info.value)
    assert "3604" in str(exc_info.value)
    assert "find_available_sessions" in str(exc_info.value)
    await client.aclose()


@respx.mock
async def test_unknown_business_error_passes_through() -> None:
    respx.post(LOGIN).side_effect = login_ok()
    respx.get(CYCLES).side_effect = httpx.Response(
        400, json={"code": 3999, "message": "未收录的错误"}
    )
    client = make_client()

    with pytest.raises(BackendError, match=r"未收录的错误\(code 3999\)"):
        await client.get("/api/cycles/open")
    await client.aclose()


@respx.mock
async def test_get_timeout_retries_then_succeeds() -> None:
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.get(CYCLES)
    route.side_effect = [
        httpx.TimeoutException("boom"),
        httpx.Response(200, json={"code": 200, "message": "ok", "data": "ok"}),
    ]

    client = make_client()
    assert await client.get("/api/cycles/open") == "ok"
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_post_timeout_does_not_retry() -> None:
    """写操作非幂等,超时不自动重试——避免双写。"""
    respx.post(LOGIN).side_effect = login_ok()
    route = respx.post(f"{BASE}/api/interview/admin/preferences/1/assign")
    route.side_effect = httpx.TimeoutException("boom")

    client = make_client()
    with pytest.raises(BackendError, match="写操作不自动重试"):
        await client.post("/api/interview/admin/preferences/1/assign", json={})
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_gateway_html_error_is_actionable() -> None:
    """502 返回 HTML 网关页时,不能抛 JSON 解析栈,要给可行动文案。"""
    respx.post(LOGIN).side_effect = login_ok()
    respx.get(CYCLES).side_effect = httpx.Response(502, text="<html>Bad Gateway</html>")

    client = make_client()
    with pytest.raises(BackendError, match="非 JSON"):
        await client.get("/api/cycles/open")
    await client.aclose()


@respx.mock
async def test_concurrent_first_calls_login_once() -> None:
    """并发首调:锁保证只登录一次,两个请求都能拿到 token。"""
    login_route = respx.post(LOGIN)
    login_route.side_effect = login_ok("tok-shared")
    respx.get(CYCLES).side_effect = httpx.Response(
        200, json={"code": 200, "message": "ok", "data": 1}
    )

    client = make_client()
    results = await asyncio.gather(
        client.get("/api/cycles/open"), client.get("/api/cycles/open")
    )
    assert results == [1, 1]
    assert login_route.call_count == 1
    await client.aclose()
