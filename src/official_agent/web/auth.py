"""共享鉴权依赖:官网 JWT → 身份解析 → 权限码校验(全仓唯一出处)。

- authenticate:Bearer JWT →(身份, 原始 JWT)。凭证无效 401;
  后端网络故障 503(可重试,不与凭证错误混为 401)。
- require_any(*codes):依赖工厂,任一权限码命中即放行;403 文案列出
  所需码。单码站点直接 require_any("code"),不再各写一份副本。

routes 与各 admin 路由模块一律从这里取依赖,禁止跨模块 import
routes 的私有鉴权符号。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, Request

from official_agent.graphs.identity import ResolvedIdentity, resolve
from official_agent.tools.client import BackendError, BackendUnavailableError


async def authenticate(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> tuple[ResolvedIdentity, str]:
    """Authorization: Bearer <官网JWT> → (身份, 官网JWT)。

    官网 JWT 两用:① resolve 换身份(只查 /auth/me)② 原样绑定给工具
    查本人数据(get_as_user 裸发)。JWT 只存会话态,不进 checkpointer。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token 为空")

    try:
        identity = await resolve({"kind": "web", "token": token})  # type: ignore[typeddict-item]
    except BackendUnavailableError as exc:
        # 后端网络/服务故障是 503(可重试),不与凭证错误混为 401
        raise HTTPException(status_code=503, detail="认证服务暂时不可用,请稍后重试") from exc
    except BackendError as exc:  # 凭证无效/过期等
        raise HTTPException(status_code=401, detail="身份解析失败") from exc
    return identity, token


def require_any(*codes: str):
    """任一权限码通过即放行的依赖工厂(403 文案列出所需码)。"""

    async def dep(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> ResolvedIdentity:
        identity, _ = await authenticate(request, authorization)
        owned = identity.get("permission_codes") or []
        if not any(c in owned for c in codes):
            raise HTTPException(status_code=403, detail=f"需要 {' 或 '.join(codes)} 权限")
        return identity

    return dep
