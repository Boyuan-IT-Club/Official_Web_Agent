"""身份解析(GRA-01):凭证 → 后端用户/角色/权限,确定性,不调模型。

架构契约(凭证红线):凭证在**入口层**(CLI/官网 handler)解析成身份结果,
只把 user_id/role/permission_codes 放进图 state——凭证本身绝不进
state/checkpointer(落库即泄漏面,SEC-07 thread 契约亦禁止)。

三通道(issue #20):
- cli(M1):账号密码 → BackendClient.login() → 解 JWT claims
  (login 响应仅返回 token,身份在 claims;roleNames 为中文角色名)
- web(预留):官网会话 JWT → GET /api/auth/me(SEC-01 后端增量,落地后实现;
  agent 不持 JWT_SECRET)
- feishu(预留):open_id → 用户映射(M3,INF-05 前置)

角色映射(A 通道语义,装配细则在 SEC-02):
- admin=读全量+写操作(经 interrupt) / member=内部只读面 / candidate=只读自己
"""

import base64
import json
from typing import Literal, TypedDict

from boyuan_agent.tools.client import BackendClient, BackendError
from boyuan_agent.tools.readonly import get_backend_client, set_backend_client

Role = Literal["admin", "member", "candidate", "unknown"]


class IdentityCredential(TypedDict, total=False):
    """入口层持有的凭证。绝不进图 state。"""

    kind: Literal["cli", "web", "feishu"]
    username: str  # cli
    password: str  # cli
    token: str  # web:官网会话 JWT(待 /me 落地)


class ResolvedIdentity(TypedDict):
    """解析结果。可进 state/checkpointer(不含凭证)。"""

    user_id: int | None  # claims 缺失/非法时 None→图节点降级 unknown
    role: Role
    role_names: list[str]  # 后端原始角色名(中文),供审计与展示
    permission_codes: list[str]  # 供 SEC-02 工具装配
    source: str  # cli / web / feishu


# 后端角色名(V6 种子):超级管理员/管理员/社员/申请人——按中文名映射,
# 多角色取权限最高者(admin > member > candidate)
_ROLE_PRIORITY: dict[str, Role] = {
    "超级管理员": "admin",
    "管理员": "admin",
    "社员": "member",
    "申请人": "candidate",
}


async def resolve(credential: IdentityCredential) -> ResolvedIdentity:
    """入口层调用:凭证 → 身份。失败抛 BackendError(凭证错/后端不可达)。"""
    kind = credential.get("kind") or "cli"
    if kind == "cli":
        return await _resolve_cli(credential)
    if kind == "web":
        raise NotImplementedError(
            "官网通道身份解析待 SEC-01 落地(GET /api/auth/me),见 issue #52"
        )
    raise NotImplementedError("飞书通道待 M3(open_id→用户映射),见 issue #5")


async def _resolve_cli(credential: IdentityCredential) -> ResolvedIdentity:
    username = credential.get("username")
    if username:
        # CLI 模拟身份:专用 client 以该账号登录;登录成功后注册为共享单例,
        # 后续工具调用都以此身份进行(登录失败不污染单例)。
        # ⚠ 进程级 last-login-wins:CLI 单用户/MCP 独立进程下安全;多会话
        # 宿主(SSE/飞书)落地时须改为按会话持有 client(SEC-07/GRA-04)
        client = _client_as(username, credential.get("password") or "")
        try:
            token = await client.login()
            claims = _decode_jwt_payload(token)
            resolved = _identity_from_claims(claims, "cli")
        except BaseException:
            # 登录失败/claims 解码失败:新建 client 关闭即弃,单例不动
            # (解码失败若先换单例,会留下半激活身份且旧 client 已关无法回滚)
            await client.aclose()
            raise
        old = await get_backend_client()
        set_backend_client(client)
        if old is not client:
            await old.aclose()
        return resolved
    else:
        # 未指定模拟账号:沿用 .env 服务账号(PAT 落地前的过渡)
        client = await get_backend_client()
        token = await client.login()
        claims = _decode_jwt_payload(token)
        return _identity_from_claims(claims, "cli")


def _identity_from_claims(claims: dict, source: str) -> ResolvedIdentity:
    user_id = claims.get("userId")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        user_id = None
    return ResolvedIdentity(
        user_id=user_id,
        role=_map_role(claims.get("roleNames") or []),
        role_names=claims.get("roleNames") or [],
        permission_codes=claims.get("permissionCodes") or [],
        source=source,
    )


def _client_as(username: str, password: str) -> BackendClient:
    """以模拟身份凭证构造独立 client(覆盖服务账号凭证)。"""
    from boyuan_agent.config import get_settings

    settings = get_settings().model_copy(
        update={"backend_service_username": username, "backend_service_password": password}
    )
    return BackendClient(settings=settings)


def _decode_jwt_payload(token: str) -> dict:
    """解 JWT payload 段(base64url)。不验签:token 刚从后端换来,传输即信任源;
    验签需要 JWT_SECRET,agent 永不持有(ADR-0006)。
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise BackendError("token 格式非法(非 JWT 三段式)")
    padding = "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise BackendError(f"token payload 解码失败: {exc}") from None
    if not isinstance(payload, dict):
        raise BackendError("token payload 非对象")
    return payload


def _map_role(role_names: list[str]) -> Role:
    """中文角色名 → A 通道角色。多角色取最高;无映射→unknown(装配层降级只读)。"""
    for role_name, mapped in _ROLE_PRIORITY.items():
        if role_name in role_names:
            return mapped
    return "unknown"
