"""GRA-05 写操作 interrupt 确认封装。

写工具执行前必须经人工确认(ADR-0005):工具函数内调用 `require_confirmation()`
挂起图,携带人类可读的操作摘要;用户批准/拒绝后恢复,恢复值作为确认决策。

机制:
- 在 LangGraph 图执行上下文中 → `interrupt()` 挂起,返回用户决策(approve/reject)
- 非图上下文(直接调用/单测)→ `interrupt()` 抛异常 → 降级抛 `ConfirmationRequired`

写路径 fail-closed(ADR-0005):无法挂起确认 = 拒绝写操作,绝不静默放行。
"""
from langgraph.types import interrupt


class ConfirmationRequired(Exception):
    """缺少或不匹配的人工确认令牌。写操作必须先经 interrupt 确认(GRA-05)。"""


# 确认决策的合法值(与 UI/飞书卡片对齐)
APPROVE = "approve"
REJECT = "reject"


def require_confirmation(summary: str) -> str:
    """挂起图请求人工确认,返回用户决策(approve/reject)。

    summary 必须是人可读的操作摘要(如「将把简历 #12 调剂到周六上午场次」)。

    图上下文内:interrupt() 自行挂起(抛 GraphInterrupt 由 LangGraph 捕获),
    恢复后返回 resume 值——不要 try/except 干扰,否则破坏恢复匹配。
    非图上下文:interrupt() 抛 RuntimeError,由调用方转 ConfirmationRequired。
    """
    decision = interrupt({"summary": summary, "confirm": True})
    if decision not in (APPROVE, REJECT):
        raise ConfirmationRequired(f"非法确认决策:{decision!r}(仅接受 approve/reject)")
    return decision


def is_operation_approved(summary: str) -> bool:
    """便捷封装:只关心批准与否(拒绝/取消都视为不执行)。"""
    return require_confirmation(summary) == APPROVE