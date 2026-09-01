"""只读工具集(TOOL-03)。端点签名已按 openapi 逐条核对(2026-08-24 漂移修正)。

docstring 契约(ADR-0003):写「何时用」而非「能做什么」,列边界条件,
关键工具附真实调用示例。docstring 即模型看到的工具描述。

返回投影(TOOL-05 #16 的原则在本文件先行应用最小集):
- 长列表透传后端的摘要语义(简历是字段值表驱动,字段白名单留 #16
  用真实字段键统一裁剪,这里不猜字段名);
- 聚合工具(get_recruit_statistics)只回计数与下钻提示,不回明细。

PII 红线(#68):本层返回原文即 trace 上报原文。get_resume_detail 是
最大暴露面(完整简历),评估流水线接入观测前必须先拍板脱敏边界。
"""

import asyncio
from typing import Any

from boyuan_agent.tools.client import BackendClient, BackendError

_client: BackendClient | None = None
_client_lock = asyncio.Lock()


async def get_backend_client() -> BackendClient:
    """进程内共享单例:token 缓存与登录锁不分裂。测试经 set_backend_client 注入。"""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = BackendClient()
    return _client


def set_backend_client(client: BackendClient | None) -> None:
    """测试注入/进程结束时替换或清空单例。"""
    global _client
    _client = client


async def get_open_cycle() -> dict | list:
    """查询当前开放的招募周期。多数查询的第一步:先拿 cycle_id 再查简历/场次。

    对应 GET /api/cycles/open。返回周期基本信息(注意:无 status 字段)。
    """
    client = await get_backend_client()
    return await client.get("/api/cycles/open")


async def search_resumes(
    cycle_id: int | None = None,
    department: str | None = None,
    name: str | None = None,
    major: str | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict:
    """按志愿部门/姓名/专业/状态分页检索简历,返回分页结构
    (content=摘要列表,totalElements=总数;不含简历全文)。

    需要单份简历详情时用 get_resume_detail 下钻。
    对应 GET /api/resumes/search(department 映射到查询参数 expectedDepartment)。
    示例:search_resumes(cycle_id=2, department="技术部", status="1")。
    """
    client = await get_backend_client()
    params: dict[str, Any] = {"page": page, "size": size}
    if cycle_id is not None:
        params["cycleId"] = cycle_id
    if department is not None:
        params["expectedDepartment"] = department
    if name is not None:
        params["name"] = name
    if major is not None:
        params["major"] = major
    if status is not None:
        params["status"] = status
    data = await client.get("/api/resumes/search", params=params)
    # 后端分页为 Spring Page 结构:content/totalElements(2026-09-01 冒烟核实)
    if data is None:
        return {"content": [], "totalElements": 0}
    return data


async def get_resume_detail(user_id: int, cycle_id: int) -> dict:
    """按「用户 + 周期」取单份简历的完整字段值(管理员视角)。

    对应 GET /api/resumes/admin/{userId}/{cycleId}(不存在按 resumeId 直查的端点;
    resumeId 需先经 search_resumes 拿到对应 userId)。
    ⚠ 返回完整简历内容(含手机号/学号等 PII),输出会被 trace 记录——
    不要在面向候选人的回答里复述这些字段。
    """
    client = await get_backend_client()
    return await client.get(f"/api/resumes/admin/{user_id}/{cycle_id}")


async def get_my_interview(cycle_id: int, user_token: str) -> dict | None:
    """候选人查自己在某周期的面试安排(用候选人本人令牌;cycleId 必填)。

    未投递或未分配时返回 null。对应 GET /api/interview/schedule/my?cycleId=。
    注意:本工具不经 MCP 对外暴露(需最终用户令牌,服务账号语义不适用)。
    """
    client = await get_backend_client()
    return await client.get_as_user(
        "/api/interview/schedule/my", params={"cycleId": cycle_id}, user_token=user_token
    )


async def find_available_sessions(
    cycle_id: int, dept_id: int | None = None, date: str | None = None
) -> dict | list:
    """某周期可用面试场次与剩余容量,可按部门过滤。

    对应 GET /api/interview/admin/cycles/{id}/available-sessions(后端仅支持
    deptId 过滤;date 为 YYYY-MM-DD 时在客户端过滤后返回)。
    """
    client = await get_backend_client()
    params: dict[str, Any] = {}
    if dept_id is not None:
        params["deptId"] = dept_id
    data = await client.get(
        f"/api/interview/admin/cycles/{cycle_id}/available-sessions", params=params
    )
    if date and isinstance(data, list):
        data = [s for s in data if str(s.get("interviewDate", "")).startswith(date)]
    return data


async def list_unassigned(cycle_id: int) -> dict | list:
    """尚未分配面试的候选人列表。对应 GET /api/interview/admin/cycles/{id}/unassigned。"""
    client = await get_backend_client()
    return await client.get(f"/api/interview/admin/cycles/{cycle_id}/unassigned")


async def list_reschedule_requests(cycle_id: int, status: int | None = 0) -> dict | list:
    """按周期查询改期申请(cycleId 必填;status:0 待处理 / 1 已同意 / 2 已拒绝,None 为全部)。

    对应 GET /api/interview/reschedule/admin/list?cycleId=&status=。
    同意改期后需人工重排:用写工具 assign_interview 调剂到新场次。
    """
    client = await get_backend_client()
    params: dict[str, Any] = {"cycleId": cycle_id}
    if status is not None:
        params["status"] = status
    return await client.get("/api/interview/reschedule/admin/list", params=params)


# 决策码语义(InterviewResultItem.decision):0 待定 / 1 通过 / 2 不通过 / 3 待调剂
_DECISION_KEYS = {0: "pending", 1: "passed", 2: "rejected", 3: "toTransfer"}


async def get_recruit_statistics(cycle_id: int) -> dict:
    """投递/面试/结果的汇总统计。

    后端无单一统计端点(原 /api/interview/statistics 未实现,已列入 SEC-01
    谈判清单),由 /api/interview/result/list(分页拉全量)与
    /api/interview/evaluation/cycles/{id}/summary 聚合计算。
    返回:总人数、按最终决定(decision)计数、按分配部门计数、已评价人数;
    看个人明细用 search_resumes / list_unassigned 下钻。
    """
    client = await get_backend_client()
    items: list[dict] = []
    page = 1
    while True:
        data = await client.get(
            "/api/interview/result/list", params={"cycleId": cycle_id, "page": page, "size": 100}
        )
        batch = (data or {}).get("interviewResults", [])
        items.extend(batch)
        total = (data or {}).get("total", len(items))
        if len(items) >= total or not batch:
            break
        page += 1

    decision_counts = {key: 0 for key in _DECISION_KEYS.values()}
    by_dept: dict[str, int] = {}
    for item in items:
        key = _DECISION_KEYS.get(item.get("decision"), "pending")
        decision_counts[key] += 1
        dept_id = item.get("assignedDeptId")
        if dept_id is not None:
            by_dept[str(dept_id)] = by_dept.get(str(dept_id), 0) + 1

    evaluated = None
    try:
        summary = await client.get(f"/api/interview/evaluation/cycles/{cycle_id}/summary")
        evaluated = len((summary or {}).get("candidates", []))
    except BackendError:
        pass  # 该周期评价表未开启时无 summary,统计不因此失败

    return {
        "cycleId": cycle_id,
        "totalResults": len(items),
        "decisionCounts": decision_counts,
        "assignedByDeptId": by_dept,
        "evaluatedCandidates": evaluated,
    }


async def get_candidate_card(
    cycle_id: int, schedule_id: int, on_behalf_of: int | None = None
) -> dict:
    """Copilot 用:候选人简历 + 该周期评价维度打包成一张卡片数据。

    对应 GET /api/interview/evaluation/cycles/{id}/candidates/{scheduleId}/resume
    与 …/dimensions。服务账号直调会因场次绑定校验被拒(2005)——需传
    on_behalf_of(面试官 userId,经 X-On-Behalf-Of 代理身份,ADR-0006);
    该机制依赖 SEC-01 后端谈判落地,在此之前本工具直调必然被拒。
    """
    client = await get_backend_client()
    headers = {"X-On-Behalf-Of": str(on_behalf_of)} if on_behalf_of else {}
    base = f"/api/interview/evaluation/cycles/{cycle_id}/candidates/{schedule_id}"
    resume = await client.get(f"{base}/resume", headers=headers)
    dimensions = await client.get(f"{base}/dimensions", headers=headers)
    return {"resume": resume, "dimensions": dimensions}
