"""写工具集:写操作经 interrupt 人工确认,以操作人本人身份执行并双录审计。

确认契约(ADR-0005):写工具执行前必须经人工确认——图内挂起(interrupt,
携带人类可读操作摘要与操作指纹),用户批准(approve)/拒绝(reject)后恢复;
批准必须带回同一指纹(tools/interrupt_guard.py)。非图上下文(直接调用)一律
按未确认处理,写路径 fail-closed。

执行顺序与失败语义(与评审采纳一致,见 web/evaluation_admin.adopt_scorecard):
1. 确认:拒绝/指纹不符 → 取消,零副作用;
2. 意图审计(fail-closed):写不进审计 → 不执行;
3. 以**操作人本人令牌**调后端(后端按本人权限判权并归因)——绝不借服务账号代写;
   超时不重试:请求可能已到达后端,重试会重复执行,如实报「结果未知」;
4. 结果审计(fail-open):写操作已发生,审计缺口记日志,不改写结果。

权限边界在装配层(graphs/assistant.assemble_tools):只有持排期/审核权限的
管理员、且会话有 checkpointer(能挂起)时才装配写工具。每次执行记审计行
(字段契约见 ADR-0006)。

move_interview 已砍除(后端 manual-adjust 端点 deprecated,
改用 assign_interview → preferences/{resumeId}/assign)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from official_agent.tools.client import BackendAuthError, BackendError, BackendUnavailableError
from official_agent.tools.interrupt_guard import (
    APPROVE,
    ConfirmationRequired,
    require_confirmation,
)

#: 审计里的执行体标识(ADR-0006 agent 字段)
_AGENT = "assistant"


def _confirm(tool: str, args: dict[str, Any], summary: str) -> str | None:
    """统一确认入口:返回 None = 已批准;否则返回取消原因(给模型的可行动文案)。

    图内挂起等用户决策;非图上下文、指纹不符一律视为未确认(fail-closed)。
    """
    try:
        decision = require_confirmation(summary, tool=tool, args=args)
    except ConfirmationRequired as exc:
        return str(exc)
    except RuntimeError:  # 非图上下文:interrupt 无法挂起
        return "此操作需要人工确认,但当前不在确认流程内,未执行"
    if decision != APPROVE:
        return "用户取消了操作,未执行"
    return None


def _call_context() -> tuple[str, int]:
    """当前图调用的 (thread_id, 操作人 user_id)——审计的「谁、在哪个会话」。"""
    from langgraph.config import get_config

    config = get_config()
    thread_id = str((config.get("configurable") or {}).get("thread_id") or "")
    user_id = (config.get("metadata") or {}).get("user_id")
    return thread_id, int(user_id or 0)


async def _execute(
    tool: str,
    args: dict[str, Any],
    summary: str,
    *,
    method: str,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """确认 → 意图审计 → 本人身份执行 → 结果审计。返回 {status, message}。"""
    from official_agent.state import audit
    from official_agent.tools.readonly import current_asker_token, get_backend_client

    cancelled = _confirm(tool, args, summary)
    if cancelled is not None:
        return {"status": "cancelled", "message": cancelled}

    token = current_asker_token()
    if not token:
        return {"status": "error", "message": "缺少操作人本人身份,写操作不能代为执行,未执行"}
    thread_id, user_id = _call_context()
    action = {"op": tool, **args}

    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=thread_id,
            acting_user_id=user_id,
            channel="web",
            agent=_AGENT,
            action=action,
            decision=f"u{user_id}:approve",
            decision_summary=summary,
            result="已确认,开始执行",
        )
    except Exception:  # noqa: BLE001 — 意图审计失败 → 不执行(fail-closed)
        logging.getLogger(__name__).warning("写操作意图审计失败 tool=%s", tool, exc_info=True)
        return {"status": "error", "message": "审计服务暂不可用,为保证可追溯,操作未执行,请稍后重试"}

    try:
        client = await get_backend_client()
        await client.request_as_user(method, path, json=body, user_token=token)
    except BackendAuthError:
        raise  # 登录失效:入口层据此提示重新登录
    except BackendUnavailableError:
        outcome: dict[str, Any] = {
            "status": "unknown",
            "message": "请求发出后与后端失去联系,操作可能已经生效。请到管理页面核实,不要重复执行",
        }
    except BackendError as exc:
        outcome = {"status": "error", "message": f"后端拒绝了操作,未生效:{exc}"}
    else:
        outcome = {"status": "done", "message": f"已完成:{summary}"}

    try:
        await asyncio.to_thread(
            audit.write_audit,
            thread_id=thread_id,
            acting_user_id=user_id,
            channel="web",
            agent=_AGENT,
            action=action,
            decision=f"u{user_id}:approve",
            decision_summary=summary,
            result=f"{outcome['status']}:{outcome['message']}"[:500],
        )
    except Exception:  # noqa: BLE001 — 结果审计 fail-open:操作已发生,不能改写结果
        logging.getLogger(__name__).warning(
            "写操作结果审计失败 tool=%s status=%s,审计缺口需人工补记",
            tool,
            outcome["status"],
            exc_info=True,
        )
    return outcome


async def assign_interview(resume_id: int, target_session_id: int) -> dict:
    """把候选人(按简历 ID)分配/再分配到目标面试场次;已有安排会先释放原场次。

    何时用:管理员要给候选人排面试、或同意改期后重排时。先用 find_available_sessions
    查到有余量的 sessionId,再调本工具;不要猜 sessionId。
    ⚠ 写操作:调用后会弹出确认卡片,用户确认后才执行;返回 status=cancelled
    表示用户取消或未确认,不要重复发起。场次已满时返回后端的可行动文案。
    对应 POST /api/interview/admin/preferences/{resumeId}/assign。
    """
    return await _execute(
        "assign_interview",
        {"resume_id": resume_id, "target_session_id": target_session_id},
        f"把简历 #{resume_id} 分配到面试场次 #{target_session_id}(已有安排会先释放原场次)",
        method="POST",
        path=f"/api/interview/admin/preferences/{resume_id}/assign",
        body={"targetSessionId": target_session_id},
    )


async def handle_reschedule(request_id: int, status: int, admin_note: str = "") -> dict:
    """处理一条改期申请:status 1 同意 / 2 拒绝,可附管理员备注。

    何时用:管理员要处理 list_reschedule_requests 里待处理(status=0)的申请时。
    同意只会取消原安排,不会自动重排——之后需要用 assign_interview 排到新场次。
    ⚠ 写操作:调用后会弹出确认卡片,用户确认后才执行。
    对应 PUT /api/interview/reschedule/admin/{id}/handle。
    """
    if status not in (1, 2):
        # 操作摘要是给用户看的安全界面(ADR-0005):非 1/2 不许渲染成「拒绝」,
        # 直接拒——标签必须与将执行的动作一致,否则用户可能误批。
        return {"status": "error", "message": f"无效的改期处理状态:{status}(仅 1 同意/2 拒绝)"}
    action = "同意" if status == 1 else "拒绝"
    note = admin_note.strip()
    return await _execute(
        "handle_reschedule",
        {"request_id": request_id, "status": status, "admin_note": note},
        f"{action}改期申请 #{request_id}(备注:{note or '无'})"
        + (";同意后原面试安排会被取消,需要再排新场次" if status == 1 else ""),
        method="PUT",
        path=f"/api/interview/reschedule/admin/{request_id}/handle",
        body={"status": status, "adminNote": note},
    )


#: 对话里可装配的写工具(名字 → 函数)。评分写回不在此列:评分走评审页的
#: 采纳流程(多人打分、盲评规则),不经对话令牌(ADR-0005)。
WRITE_TOOLS = {
    "assign_interview": assign_interview,
    "handle_reschedule": handle_reschedule,
}

__all__ = ["WRITE_TOOLS", "ConfirmationRequired", "assign_interview", "handle_reschedule"]
