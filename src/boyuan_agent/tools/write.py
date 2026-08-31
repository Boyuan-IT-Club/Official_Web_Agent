"""写工具集(TOOL-04):全部强制校验 interrupt 确认令牌。

令牌契约(ADR-0005):唯一发行方是 LangGraph interrupt 恢复路径
(Command(resume=...) 的恢复值即令牌);令牌绑定操作指纹
hash(工具名+关键参数),一次性,执行即作废;拒绝也走恢复流程。
权限边界在这一层的代码里,不依赖模型自觉。每次执行记审计行(SEC-03,
字段契约见 ADR-0006)。

move_interview 已砍除(后端 manual-adjust 端点 deprecated,
改用 assign_interview → preferences/{resumeId}/assign)。
"""


class ConfirmationRequired(Exception):
    """缺少或不匹配的人工确认令牌。写操作必须先经 interrupt 确认(GRA-05)。"""


def _require_token(confirmation_token: str | None) -> None:
    if not confirmation_token:
        raise ConfirmationRequired("此操作需要人工确认令牌,请先经 interrupt 确认流程")
    # TODO(TOOL-04): 指纹校验——令牌须对应当前 thread 挂起记录的操作指纹
    # hash(工具名+关键参数),不一致即拒;一次性,执行即作废(ADR-0005)。


async def assign_interview(
    resume_id: int, target_session_id: int, confirmation_token: str | None = None
) -> dict:
    """把候选人(按简历 ID)分配/再分配到目标场次;已有安排则先释放原场次。

    场次已满返回业务码 3604 的可行动文案。同意改期后的重排也走本工具。
    对应 POST /api/interview/admin/preferences/{resumeId}/assign。⚠ 写操作。
    """
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")


async def handle_reschedule(
    request_id: int, status: int, admin_note: str = "", confirmation_token: str | None = None
) -> dict:
    """处理改期申请:status 1 同意 / 2 拒绝,可附管理员备注。

    同意仅取消原安排,不自动重排——随后用 assign_interview 调剂到新场次。
    对应 PUT /api/interview/reschedule/admin/{id}/handle。⚠ 写操作。
    """
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")


async def submit_resume_score(
    resume_id: int, score: int, confirmation_token: str | None = None
) -> dict:
    """写回简历评分(整数 0~100,落库并署名打分人)。

    对应 PUT /api/resumes/{id}/score(body 仅 score;维度分与依据的落库通道
    尚不存在,已列入 SEC-01 后端谈判清单)。⚠ 写操作;B 流水线批量写回走
    评审确认语义,不经本对话令牌(ADR-0005)。
    """
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")
