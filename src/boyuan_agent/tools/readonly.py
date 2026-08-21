"""只读工具集(TOOL-03),映射见设计方案 §05 工具表。

每个工具的 docstring 会成为模型看到的工具描述,写清:何时用、参数取值、返回什么、
与相近工具的区别。
"""


async def get_open_cycle() -> dict:
    """查询当前开放的招新周期(cycle)。

    多数查询的第一步:后续按 cycle_id 查简历/场次。返回 {cycle_id, name, status, ...}。
    对应 GET /api/cycles/open。
    """
    raise NotImplementedError("TOOL-03")


async def search_resumes(
    cycle_id: int,
    department: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> dict:
    """按部门/状态/关键词检索简历,返回摘要列表 + 总数(不含简历全文)。

    需要单份简历详情时用 get_resume_detail 下钻。对应 POST /api/resumes/search。
    """
    raise NotImplementedError("TOOL-03")


async def get_resume_detail(resume_id: int) -> dict:
    """单份简历的完整字段值。对应 GET /api/resumes/{id} 等。"""
    raise NotImplementedError("TOOL-03")


async def get_my_interview(user_token: str) -> dict:
    """候选人查自己的面试安排与状态(用候选人本人令牌)。对应 GET /api/interview/schedule/my。"""
    raise NotImplementedError("TOOL-03")


async def find_available_sessions(cycle_id: int, date: str | None = None) -> dict:
    """某周期(可选某天,YYYY-MM-DD)可用面试场次与剩余容量。

    对应 GET /api/interview/admin/cycles/{id}/available-sessions + 过滤。
    """
    raise NotImplementedError("TOOL-03")


async def list_unassigned(cycle_id: int) -> dict:
    """尚未分配面试的候选人列表。对应 GET /api/interview/admin/cycles/{id}/unassigned。"""
    raise NotImplementedError("TOOL-03")


async def list_reschedule_requests(status: str = "pending") -> dict:
    """待处理的改期申请。对应 GET /api/interview/reschedule/admin/list。"""
    raise NotImplementedError("TOOL-03")


async def get_recruit_statistics(cycle_id: int) -> dict:
    """投递/面试/结果统计。对应 GET /api/interview/statistics 等。"""
    raise NotImplementedError("TOOL-03")


async def get_candidate_card(cycle_id: int, schedule_id: int) -> dict:
    """Copilot 用:候选人简历 + 该周期评价维度打包成一张卡片数据。

    对应 …/evaluation/cycles/{id}/candidates/{scheduleId}/resume + …/dimensions。
    """
    raise NotImplementedError("TOOL-03")
