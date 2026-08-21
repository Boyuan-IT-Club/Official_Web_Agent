"""写工具集(TOOL-04):全部强制校验 interrupt 确认令牌,无令牌抛错。

权限边界在这一层的代码里,不依赖模型自觉。每次执行记审计日志(SEC-03)。
"""


class ConfirmationRequired(Exception):
    """缺少人工确认令牌。写操作必须先经 LangGraph interrupt 获得确认(GRA-05)。"""


def _require_token(confirmation_token: str | None) -> None:
    if not confirmation_token:
        raise ConfirmationRequired("此操作需要人工确认令牌,请先经 interrupt 确认流程")
    # TODO(TOOL-04): 校验令牌真实性(与 checkpointer 中挂起的 interrupt 匹配、一次性)


async def move_interview(
    schedule_id: int, session_id: int, confirmation_token: str | None = None
) -> dict:
    """调整某候选人的面试到指定场次。对应 POST /api/interview/schedule/manual-adjust。⚠ 写操作。"""
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")


async def handle_reschedule(
    request_id: int, approve: bool, reason: str, confirmation_token: str | None = None
) -> dict:
    """批准/驳回改期申请。对应 POST /api/interview/reschedule/admin/{id}/handle。⚠ 写操作。"""
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")


async def submit_resume_score(
    resume_id: int, score: float, comment: str, confirmation_token: str | None = None
) -> dict:
    """写回评估流水线的评分。对应 PUT /api/resumes/{id}/score。⚠ 写操作。"""
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")
