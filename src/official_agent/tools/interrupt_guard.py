"""写操作 interrupt 确认封装。

写工具执行前必须经人工确认(ADR-0005):工具函数内调用 `require_confirmation()`
挂起图,携带人类可读的操作摘要;用户批准/拒绝后恢复,恢复值作为确认决策。

机制:
- 在 LangGraph 图执行上下文中 → `interrupt()` 挂起,返回用户决策(approve/reject)
- 非图上下文(直接调用/单测)→ `interrupt()` 抛异常 → 降级抛 `ConfirmationRequired`

确认令牌(ADR-0005「唯一发行方是 interrupt 恢复路径」):
- 挂起载荷带操作指纹 = hash(工具名 + 关键参数);
- 批准必须以 {"decision": "approve", "fingerprint": <同一指纹>} 恢复——指纹对不上
  (旧卡片、篡改的请求、模型换了参数重新调用)一律拒绝;
- 一次性由 LangGraph 保证:同一个挂起点只能被恢复一次,恢复后图继续前进;
- 拒绝不需要令牌:任何形式的 reject 都按取消处理。

写路径 fail-closed(ADR-0005):无法挂起确认 = 拒绝写操作,绝不静默放行。
"""

import hashlib
import json
from typing import Any

from langgraph.types import interrupt


class ConfirmationRequired(Exception):
    """缺少或不匹配的人工确认令牌。写操作必须先经 interrupt 确认。"""


# 确认决策的合法值(与 UI/飞书卡片对齐)
APPROVE = "approve"
REJECT = "reject"


def operation_fingerprint(tool: str, args: dict[str, Any]) -> str:
    """操作指纹:工具名 + 关键参数的稳定哈希(参数按键排序,与调用顺序无关)。"""
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def require_confirmation(
    summary: str, *, tool: str | None = None, args: dict[str, Any] | None = None
) -> str:
    """挂起图请求人工确认,返回用户决策(approve/reject)。

    summary 必须是人可读的操作摘要(如「将把简历 #{resume_id} 调剂到周六上午场次」)。
    传入 tool/args 即进入**令牌模式**:挂起载荷带操作指纹,批准必须带回同一指纹。
    不传则只认 approve/reject 字符串(无真实写入的演示/测试路径)。

    图上下文内:interrupt() 自行挂起(抛 GraphInterrupt 由 LangGraph 捕获),
    恢复后返回 resume 值——不要 try/except 干扰,否则破坏恢复匹配。
    非图上下文:interrupt() 抛 RuntimeError,由调用方转 ConfirmationRequired。
    NOTE: 脆弱点在 write.py 的 `except RuntimeError`——那是对「interrupt 无法
    挂起」的降级;将来任何人在这加宽 except 吞掉 GraphInterrupt,恢复匹配即坏。
    """
    # 出口契约:挂起载荷先过 mask_pii(summary 会进 checkpointer 挂起态)
    from official_agent.security.pii import mask_pii

    payload: dict[str, Any] = {"summary": mask_pii(summary), "confirm": True}
    fingerprint = None
    if tool is not None:
        fingerprint = operation_fingerprint(tool, args or {})
        payload.update(tool=tool, fingerprint=fingerprint)
    resumed = interrupt(payload)
    return _decision_of(resumed, fingerprint)


def _decision_of(resumed: Any, fingerprint: str | None) -> str:
    """恢复值 → 决策。令牌模式下批准必须带回同一指纹。"""
    if isinstance(resumed, dict):
        decision = resumed.get("decision")
        token = resumed.get("fingerprint")
    else:
        decision, token = resumed, None
    if decision not in (APPROVE, REJECT):
        raise ConfirmationRequired(f"非法确认决策:{decision!r}(仅接受 approve/reject)")
    if decision == APPROVE and fingerprint is not None and token != fingerprint:
        raise ConfirmationRequired("确认凭证与待执行的操作不一致(可能是过期的确认卡片),未执行")
    return decision
