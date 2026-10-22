"""工具信封:业务错误转可行动结构,登录失效与程序缺陷照常上抛。"""

import inspect

import pytest

from official_agent.tools.client import BackendAuthError, BackendError, BackendUnavailableError
from official_agent.tools.envelope import mount_error_envelope


def _tool(behaviour):
    async def lookup(cycle_id: int) -> object:
        """查点什么。"""
        return behaviour()

    return mount_error_envelope(lookup)


async def test_success_wrapped_as_ok() -> None:
    assert await _tool(lambda: [1, 2])(cycle_id=1) == {"status": "ok", "data": [1, 2]}


async def test_result_with_own_status_passes_through() -> None:
    own = {"status": "not_found", "results": []}
    assert await _tool(lambda: own)(cycle_id=1) == own


async def test_business_error_becomes_actionable_payload() -> None:
    def boom():
        raise BackendError("该周期尚未开放")

    result = await _tool(boom)(cycle_id=1)
    assert result["status"] == "error"
    assert result["message"] == "该周期尚未开放"
    assert "不要编造" in result["hint"]


async def test_unavailable_is_its_own_status() -> None:
    def boom():
        raise BackendUnavailableError("后端连不上")

    assert (await _tool(boom)(cycle_id=1))["status"] == "unavailable"


async def test_auth_error_still_raises() -> None:
    """登录失效要冒到入口层,前端才会弹重新登录。"""

    def boom():
        raise BackendAuthError("用户令牌无效或已过期")

    with pytest.raises(BackendAuthError):
        await _tool(boom)(cycle_id=1)


async def test_programming_error_still_raises() -> None:
    def boom():
        raise KeyError("bug")

    with pytest.raises(KeyError):
        await _tool(boom)(cycle_id=1)


def test_signature_and_doc_preserved() -> None:
    """工具 schema 从签名派生:包装后模型看到的参数与描述不能变。"""
    wrapped = _tool(lambda: None)
    assert list(inspect.signature(wrapped).parameters) == ["cycle_id"]
    assert wrapped.__doc__ == "查点什么。"
    assert wrapped.__name__ == "lookup"
