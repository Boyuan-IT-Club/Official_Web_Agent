"""SEC-09 凭证保管单测:存取/过期/优先级链/auth_status 无凭证泄漏。"""

import base64
import json
import time

import httpx
import pytest
import respx

from boyuan_agent import credentials
from boyuan_agent.config import Settings
from boyuan_agent.tools import readonly
from boyuan_agent.tools.client import BackendAuthError, BackendClient


def make_jwt(user_id: int = 1, exp: int | None = None, role: str = "超级管理员") -> str:
    def b64(seg: bytes) -> str:
        return base64.urlsafe_b64encode(seg).decode().rstrip("=")

    claims: dict = {"userId": user_id, "roleNames": [role]}
    if exp is not None:
        claims["exp"] = exp
    return f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}.sig"


def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        backend_base_url="http://backend.test",
        backend_service_username="",
        backend_service_password="",
    )


@pytest.fixture
def cred_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(credentials, "_CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(credentials, "_CREDENTIALS_DIR", tmp_path)
    yield


def test_save_load_roundtrip_and_expiry(cred_file, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials.save("tok-1", exp=int(time.time()) + 3600, user_id=1, username="admin")
    loaded = credentials.load()
    assert loaded is not None and loaded["token"] == "tok-1" and loaded["user_id"] == 1

    # 过期→视为不存在
    credentials.save("tok-old", exp=int(time.time()) - 10)
    assert credentials.load() is None
    # 但 raw 仍可读(auth_status 需要提示「已过期,请重新 login」)
    raw = credentials.load_raw()
    assert raw is not None and raw["exp"] <= time.time()

    credentials.clear()
    assert credentials.load() is None
    assert credentials.load_raw() is None


def test_saved_file_permission_is_600(cred_file) -> None:
    import stat

    credentials.save("tok", exp=int(time.time()) + 60)
    mode = stat.S_IMODE(credentials._CREDENTIALS_FILE.stat().st_mode)
    assert mode == 0o600


def make_client() -> BackendClient:
    return BackendClient(
        http=httpx.AsyncClient(base_url="http://backend.test"), settings=settings()
    )


@respx.mock
async def test_stored_token_takes_priority(cred_file) -> None:
    """存储 token 有效:直接使用,不发 login。"""
    login_route = respx.post("http://backend.test/api/auth/login")
    api_route = respx.get("http://backend.test/api/cycles/open").mock(
        side_effect=lambda _: httpx.Response(200, json={"code": 200, "message": "ok", "data": 1})
    )
    credentials.save("stored-tok", exp=int(time.time()) + 3600)

    client = make_client()
    data = await client.get("/api/cycles/open")

    assert data == 1
    assert not login_route.called  # 存储命中时不登录
    assert api_route.calls.last.request.headers["Authorization"] == "Bearer stored-tok"
    await client.aclose()


@respx.mock
async def test_no_credentials_and_no_env_gives_login_guidance(cred_file) -> None:
    """P0 场景:无存储 token 且 .env 无账密 → 可行动指引(而非裸栈)。"""
    respx.post("http://backend.test/api/auth/login")
    client = make_client()

    with pytest.raises(BackendAuthError, match="boyuan-agent login"):
        await client.get("/api/cycles/open")
    await client.aclose()


@respx.mock
async def test_expired_storage_falls_back_to_env_login(cred_file) -> None:
    """存储过期+.env 有账密:走 login,不误用过期 token。"""
    import base64
    import json

    def b64(seg: bytes) -> str:
        return base64.urlsafe_b64encode(seg).decode().rstrip("=")

    claims = {"userId": 1, "roleNames": ["管理员"]}
    token = f"{b64(b'{}')}.{b64(json.dumps(claims).encode())}.sig"
    respx.post("http://backend.test/api/auth/login").mock(
        side_effect=lambda _: httpx.Response(
            200, json={"code": 200, "message": "ok", "data": {"token": token}}
        )
    )
    respx.get("http://backend.test/api/cycles/open").mock(
        side_effect=lambda _: httpx.Response(200, json={"code": 200, "message": "ok", "data": 1})
    )
    credentials.save("expired", exp=int(time.time()) - 5)

    client = make_client()  # settings 这里带账密
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        backend_base_url="http://backend.test",
        backend_service_username="admin",
        backend_service_password="pw",
    )
    client2 = BackendClient(
        http=httpx.AsyncClient(base_url="http://backend.test"), settings=settings
    )
    assert await client2.get("/api/cycles/open") == 1
    assert (await readonly.get_backend_client()).token != "expired"
    await client.aclose()
    await client2.aclose()


def test_auth_status_has_no_credentials_leak(cred_file) -> None:
    """红线断言:auth_status 输出不含 token 本身,只含身份与剩余时长。"""
    from boyuan_agent.tools.credentials import auth_status

    credentials.save("secret-token-value", exp=int(time.time()) + 3600, user_id=1, username="admin")
    status = auth_status()
    dumped = json.dumps(status, ensure_ascii=False)
    assert "secret-token-value" not in dumped
    assert status["authenticated"] is True
    assert status["user_id"] == 1 and status["username"] == "admin"
    assert 55 <= status["remaining_minutes"] <= 60  # save→load 间会流逝秒级


def test_auth_status_reports_expired(cred_file) -> None:
    from boyuan_agent.tools.credentials import auth_status

    credentials.save("expired", exp=int(time.time()) - 5)
    status = auth_status()
    assert status["expired"] is True and status["authenticated"] is False
    assert "boyuan-agent login" in status["hint"]


def test_auth_status_when_never_logged_in(cred_file) -> None:
    from boyuan_agent.tools.credentials import auth_status

    status = auth_status()
    assert status["authenticated"] is False
    assert "boyuan-agent login" in status["hint"]
