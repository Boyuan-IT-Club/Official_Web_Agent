"""只读工具集(TOOL-03)。端点签名已按 openapi 逐条核对(2026-08-24 漂移修正)。

docstring 契约(ADR-0003):写「何时用」而非「能做什么」,列边界条件,
关键工具附真实调用示例。docstring 即模型看到的工具描述。
"""


async def get_open_cycle() -> dict:
    """查询当前开放的招募周期。多数查询的第一步:先拿 cycle_id 再查简历/场次。

    对应 GET /api/cycles/open。返回周期基本信息(注意:无 status 字段)。
    """
    raise NotImplementedError("TOOL-03")


async def search_resumes(
    cycle_id: int | None = None,
    department: str | None = None,
    name: str | None = None,
    major: str | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """按志愿部门/姓名/专业/状态分页检索简历,返回摘要列表 + 总数(不含简历全文)。

    需要单份简历详情时用 get_resume_detail 下钻。
    对应 GET /api/resumes/search(department 映射到查询参数 expectedDepartment)。
    示例:search_resumes(cycle_id=2, department="技术部", status="1")。
    """
    raise NotImplementedError("TOOL-03")


async def get_resume_detail(user_id: int, cycle_id: int) -> dict:
    """按「用户 + 周期」取单份简历的完整字段值(管理员视角)。

    对应 GET /api/resumes/admin/{userId}/{cycleId}(不存在按 resumeId 直查的端点;
    resumeId 需先经 search_resumes 拿到对应 userId)。
    """
    raise NotImplementedError("TOOL-03")


async def get_my_interview(cycle_id: int, user_token: str) -> dict:
    """候选人查自己在某周期的面试安排(用候选人本人令牌;cycleId 必填)。

    未投递或未分配时返回 null。对应 GET /api/interview/schedule/my?cycleId=。
    注意:本工具不经 MCP 对外暴露(需最终用户令牌,服务账号语义不适用)。
    """
    raise NotImplementedError("TOOL-03")


async def find_available_sessions(
    cycle_id: int, dept_id: int | None = None, date: str | None = None
) -> dict:
    """某周期可用面试场次与剩余容量,可按部门过滤。

    对应 GET /api/interview/admin/cycles/{id}/available-sessions(后端仅支持
    deptId 过滤;date 为 YYYY-MM-DD 时在客户端过滤后返回)。
    """
    raise NotImplementedError("TOOL-03")


async def list_unassigned(cycle_id: int) -> dict:
    """尚未分配面试的候选人列表。对应 GET /api/interview/admin/cycles/{id}/unassigned。"""
    raise NotImplementedError("TOOL-03")


async def list_reschedule_requests(cycle_id: int, status: int | None = 0) -> dict:
    """按周期查询改期申请(cycleId 必填;status:0 待处理 / 1 已同意 / 2 已拒绝,None 为全部)。

    对应 GET /api/interview/reschedule/admin/list?cycleId=&status=。
    同意改期后需人工重排:用写工具 assign_interview 调剂到新场次。
    """
    raise NotImplementedError("TOOL-03")


async def get_recruit_statistics(cycle_id: int) -> dict:
    """投递/面试/结果的汇总统计。

    后端无单一统计端点(原 /api/interview/statistics 未实现,已列入 SEC-01
    谈判清单),v1 由 /api/interview/result/list 与
    /api/interview/evaluation/cycles/{id}/summary 聚合计算。
    """
    raise NotImplementedError("TOOL-03")


async def get_candidate_card(cycle_id: int, schedule_id: int) -> dict:
    """Copilot 用:候选人简历 + 该周期评价维度打包成一张卡片数据。

    对应 GET /api/interview/evaluation/cycles/{id}/candidates/{scheduleId}/resume
    与 …/dimensions。注意:服务账号直调会因场次绑定校验被拒(2005),
    需 X-On-Behalf-Of 代理身份(ADR-0006)落地后才可用。
    """
    raise NotImplementedError("TOOL-03")
