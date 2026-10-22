"""工具返回的统一信封:{status, data} / {status, message, hint}。

为什么要包:LangChain 的工具节点默认只拦参数校验错误,其余异常原样上抛——
一次后端业务报错(如「该周期尚未开放」)会让整轮对话直接失败,模型连「查不到」
都说不出来。包一层之后,业务错误变成模型能读懂、能据此行动的结构:

- ok:{"status": "ok", "data": <工具原返回>}
- error:业务错误(参数不对、对象不存在),hint 告诉模型下一步怎么做
- unavailable:后端不可达,如实告知稍后再试

**不拦**的两类:
- BackendAuthError(登录失效):入口层要据此给前端 auth_expired,让用户重新登录;
  吞成一段文字前端就弹不出重登。
- 非 BackendError 的异常:那是程序缺陷,必须向上暴露,不能伪装成「后端报错」。

已自带 status 字段的工具(search_knowledge 的 ok/not_found/unavailable)原样透传。
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from official_agent.tools.client import BackendAuthError, BackendError, BackendUnavailableError

_HINT_ERROR = "按 message 调整参数后最多重试一次;仍失败就如实告诉用户查不到,不要编造结果"
_HINT_UNAVAILABLE = "后端暂时不可用:如实告诉用户稍后再试,不要编造结果,也不要反复重试"


def mount_error_envelope(fn):
    """工具函数装饰:返回值装进信封,后端业务错误转成可行动的错误结构。

    functools.wraps 保留 __wrapped__,工具 schema 派生看到的仍是原签名。
    """

    @functools.wraps(fn)
    async def enveloped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = await fn(*args, **kwargs)
        except BackendAuthError:
            raise
        except BackendUnavailableError as exc:
            _log(fn, exc)
            return {"status": "unavailable", "message": str(exc), "hint": _HINT_UNAVAILABLE}
        except BackendError as exc:
            _log(fn, exc)
            return {"status": "error", "message": str(exc), "hint": _HINT_ERROR}
        if isinstance(result, dict) and "status" in result:
            return result
        return {"status": "ok", "data": result}

    return enveloped


def _log(fn: Any, exc: Exception) -> None:
    logging.getLogger(__name__).warning(
        "tool_error tool=%s error_class=%s", getattr(fn, "__name__", "tool"), type(exc).__name__
    )
