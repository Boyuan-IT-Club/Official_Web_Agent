"""GRA-01 身份解析单测:claims 解码/角色映射/CLI 全链路/节点校验行为。"""

import base64
import json
from typing import cast

import httpx
import pytest
import respx

from boyuan_agent.config import Settings
from boyuan_agent.graphs.identity import (
    IdentityCredential,
    ResolvedIdentity,
    _decode_jwt_payload,
    _map_role,
    resolve,
)
from boyuan_agent.graphs.router import AgentState, build_router_graph, resolve_identity
from boyuan_agent.tools import readonly
from boyuan_agent.tools.client import BackendClient, BackendError

BASE = "http://backend.test"
LOGIN = f"{BASE}/api/auth/login"


def make_jwt(claims: dict) -> str:
    def b64(seg: bytes) -> str:
        return base64.urlsafe_b64encode(seg).decode().rstrip("=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    return f"{header}.{payload}.signature"


def login_ok(claims: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 200, "message": "ok", "data": {"token": make_jwt(claims)}},
    )


ADMIN_CLAIMS = {
    "userId": 1,
    "roleNames": ["超级管理员"],
    "permissionCodes": ["user:view", "resume:view"],
}


def mock_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        backend_base_url=BASE,
        backend_service_username="svc",
        backend_service_password="secret",
    )


def install_shared_client() -> BackendClient:
    client = BackendClient(http=httpx.AsyncClient(base_url=BASE), settings=mock_settings())
    readonly.set_backend_client(client)
    return client


async def cleanup_shared_client() -> None:
    await (await readonly.get_backend_client()).aclose()
    readonly.set_backend_client(None)


def test_decode_jwt_payload_standard_claims() -> None:
    claims = _decode_jwt_payload(make_jwt(ADMIN_CLAIMS))
    assert claims["userId"] == 1
    assert claims["roleNames"] == ["超级管理员"]


def test_decode_jwt_rejects_malformed_token() -> None:
    with pytest.raises(BackendError, match="非法"):
        _decode_jwt_payload("not-a-jwt")
    with pytest.raises(BackendError, match="解码失败"):
        _decode_jwt_payload("a.!!!not-base64!!!.c")


def test_map_role_priority_and_unknown() -> None:
    assert _map_role(["管理员"]) == "admin"
    assert _map_role(["社员"]) == "member"
    assert _map_role(["申请人"]) == "candidate"
    # 多角色取最高(admin > member > candidate)——真后端 admin 即多角色
    assert _map_role(["社员", "管理员"]) == "admin"
    assert _map_role(["超级管理员", "社员"]) == "admin"
    assert _map_role(["不存在的新角色"]) == "unknown"
    assert _map_role([]) == "unknown"


@respx.mock
async def test_resolve_cli_full_chain_registers_identity_client() -> None:
    """CLI 模拟身份:专用 client 登录 → claims 解析 → 注册为共享单例。"""
    route = respx.post(LOGIN).mock(
        side_effect=lambda request: login_ok(
            {**ADMIN_CLAIMS, "roleNames": ["管理员"], "userId": 7}
        )
    )
    install_shared_client()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("boyuan_agent.config.get_settings", mock_settings)
            identity = await resolve(
                IdentityCredential(kind="cli", username="manager1", password="pass1234")
            )
        assert identity == ResolvedIdentity(
            user_id=7,
            role="admin",
            role_names=["管理员"],
            permission_codes=["user:view", "resume:view"],
            source="cli",
        )
        # 登录用的是模拟账号凭证,不是共享单例的服务账号
        sent = json.loads(route.calls.last.request.content)
        assert sent["auth_id"] == "manager1"
        # 登录成功后单例已换成该身份(后续工具调用都以此身份)
        client = await readonly.get_backend_client()
        assert client._settings.backend_service_username == "manager1"
    finally:
        await cleanup_shared_client()


@respx.mock
async def test_resolve_cli_login_failure_leaves_singleton_untouched() -> None:
    """登录失败:不污染共享单例,新建 client 也被关闭(不泄漏连接池)。"""
    install_shared_client()
    before = await readonly.get_backend_client()
    respx.post(LOGIN).mock(
        side_effect=lambda _: httpx.Response(401, json={"code": 401, "message": "用户名或密码错误"})
    )
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("boyuan_agent.config.get_settings", mock_settings)
            with pytest.raises(BackendError):
                await resolve(IdentityCredential(kind="cli", username="x", password="y"))
        # 单例仍是原 client(未被失败登录替换)
        assert await readonly.get_backend_client() is before
    finally:
        await cleanup_shared_client()


@respx.mock
async def test_resolve_cli_without_username_uses_shared_client() -> None:
    """未指定模拟账号:沿用共享单例(服务账号,过渡行为)。"""
    respx.post(LOGIN).mock(side_effect=lambda _: login_ok(ADMIN_CLAIMS))
    install_shared_client()
    try:
        identity = await resolve(IdentityCredential(kind="cli"))
        assert identity["user_id"] == 1
        assert identity["role"] == "admin"
        assert identity["source"] == "cli"
    finally:
        await cleanup_shared_client()


@respx.mock
async def test_resolve_cli_missing_user_id_degrades() -> None:
    """P2 回归:claims 缺 userId 不得兜底 0(0 会绕过 unknown 降级)。"""
    respx.post(LOGIN).mock(
        side_effect=lambda _: login_ok({"roleNames": ["管理员"], "permissionCodes": []})
    )
    install_shared_client()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("boyuan_agent.config.get_settings", mock_settings)
            identity = await resolve(IdentityCredential(kind="cli", username="ghost", password="x"))
        assert identity["user_id"] is None
        assert resolve_identity(cast(AgentState, dict(identity)))["role"] == "unknown"
    finally:
        await cleanup_shared_client()


async def test_resolve_web_and_feishu_reserved_with_guidance() -> None:
    with pytest.raises(NotImplementedError, match="api/auth/me"):
        await resolve(IdentityCredential(kind="web", token="x"))
    with pytest.raises(NotImplementedError, match="M3"):
        await resolve(IdentityCredential(kind="feishu"))


@respx.mock
async def test_resolve_kind_none_defaults_to_cli_not_feishu() -> None:
    """P3 回归:kind 缺省/None 落 cli,不误入飞书分支。"""
    respx.post(LOGIN).mock(
        side_effect=lambda _: httpx.Response(401, json={"code": 401, "message": "凭证错"})
    )
    install_shared_client()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("boyuan_agent.config.get_settings", mock_settings)
            # 走到 cli 登录(失败)而非飞书 NotImplementedError,即证明落对分支
            with pytest.raises(BackendError):
                await resolve(cast(IdentityCredential, {"username": "u", "password": "p"}))
    finally:
        await cleanup_shared_client()


def test_resolve_identity_node_passes_through_resolved() -> None:
    state: AgentState = {
        "user_id": 7,
        "role": "member",
        "permission_codes": ["resume:view"],
    }
    out = resolve_identity(state)
    assert out["role"] == "member"
    assert out["user_id"] == 7
    assert out["permission_codes"] == ["resume:view"]


def test_resolve_identity_node_unknown_fallbacks() -> None:
    # 入口没解析身份(user_id 缺失)→ unknown,装配层降级只读
    assert resolve_identity({})["role"] == "unknown"
    # 无 user_id 的 admin 声明不可信
    no_id = cast(AgentState, {"role": "admin"})
    assert resolve_identity(no_id)["role"] == "unknown"
    # 非法 role 值 → unknown
    bad = cast(AgentState, {"user_id": 3, "role": "superuser"})
    assert resolve_identity(bad)["role"] == "unknown"
    # permission_codes 缺省 → 空列表(可序列化,不 None)
    assert resolve_identity({"user_id": 3, "role": "candidate"})["permission_codes"] == []


def test_router_graph_compiles_with_identity_node() -> None:
    graph = build_router_graph().compile()
    assert "resolve_identity" in graph.get_graph().nodes
