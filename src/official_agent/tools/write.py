"""写工具集(TOOL-04 机制先行 + GRA-05):写操作经 interrupt 人工确认。

确认契约(ADR-0005):写工具执行前必须经人工确认——图内挂起(interrupt,
携带人类可读操作摘要),用户批准(approve)/拒绝(reject)后恢复;批准后仍
校验确认令牌(指纹绑定,TOOL-04 完整实现)。非图上下文(直接调用)一律
转 ConfirmationRequired,写路径 fail-closed。

权限边界在这一层的代码里,不依赖模型自觉。每次执行记审计行(SEC-03,
字段契约见 ADR-0006)。

move_interview 已砍除(后端 manual-adjust 端点 deprecated,
改用 assign_interview → preferences/{resumeId}/assign)。
"""

from official_agent.tools.interrupt_guard import ConfirmationRequired, require_confirmation


def _confirm_or_fail(summary: str) -> None:
    """统一确认入口:图内挂起等用户决策;非图上下文转 ConfirmationRequired。

    写路径 fail-closed(ADR-0005):不在确认流程内 = 拒绝写操作,绝不静默放行。
    """
    try:
        require_confirmation(summary)
    except ConfirmationRequired:
        raise
    except RuntimeError as exc:  # 非图上下文:interrupt 无法挂起
        raise ConfirmationRequired(
            "此操作需要人工确认,但当前不在图确认流程内。"
        ) from exc


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
    _confirm_or_fail(f"将把简历 #{resume_id} 分配到目标场次 #{target_session_id},请确认")
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")


async def handle_reschedule(
    request_id: int, status: int, admin_note: str = "", confirmation_token: str | None = None
) -> dict:
    """处理改期申请:status 1 同意 / 2 拒绝,可附管理员备注。

    同意仅取消原安排,不自动重排——随后用 assign_interview 调剂到新场次。
    对应 PUT /api/interview/reschedule/admin/{id}/handle。⚠ 写操作。
    """
    action = "同意" if status == 1 else "拒绝"
    _confirm_or_fail(f"将{action}改期申请 #{request_id}(备注:{admin_note or '无'}),请确认")
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
    _confirm_or_fail(f"将给简历 #{resume_id} 打 {score} 分,请确认")
    _require_token(confirmation_token)
    raise NotImplementedError("TOOL-04")