"""工具返回投影:后端原始响应 → 模型真正需要的最小字段集,纯函数零 IO。

为什么要裁:后端为管理页面设计的响应是「全量」的——简历检索的每一项都带着
整份简历字段、邮箱、打分记录。原样塞进模型上下文有两个代价:
1. token:一页 20 份简历动辄上万 token,挤掉真正的对话内容;
2. 隐私:个人信息进了上下文就进了 trace,而检索列表此前并未脱敏。

投影规则:只留回答问题要用的字段;姓名一律不进(列表用 resumeId 指代,详情
走键级掩码);人工打分不进(评分只在评审面可见,客服面不得泄露)。
"""

from __future__ import annotations

import json
from typing import Any

from official_agent.security.pii import mask_pii, mask_pii_deep

#: 报名信息字段的识别:键名优先,label 兜底(周期配置里键名不稳定)
_MAJOR_KEYS = ("major",)
_MAJOR_LABELS = ("专业",)
_GRADE_KEYS = ("grade",)
_GRADE_LABELS = ("年级",)
_DEPT_KEYS = ("expected_departments", "expected_department")
_DEPT_LABELS = ("期望部门", "志愿部门", "意向部门")

#: 不进模型的简历字段:照片/附件(值是 base64 或存储键,对回答无用且极长)
_SKIP_FIELD_TYPES = frozenset({"file", "image", "photo", "upload", "attachment"})
_SKIP_FIELD_LABEL_HINTS = ("照片", "头像", "附件", "身份证")
#: 单个字段值的上限:超长值多半是富文本或误填,截断保住上下文预算
_FIELD_VALUE_MAX = 1500


def _field_value(fields: list[dict], keys: tuple[str, ...], labels: tuple[str, ...]) -> str:
    for f in fields:
        key = str(f.get("fieldKey") or "").strip().casefold()
        label = str(f.get("fieldLabel") or "").strip()
        if key in keys or label in labels:
            value = str(f.get("fieldValue") or "").strip()
            if value:
                return value
    return ""


def _departments(raw: str) -> list[str]:
    """志愿字段原文(JSON 数组 / 逗号分隔 / 单值)→ 部门列表,去掉「无」。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        parts = [str(p) for p in parsed]
    elif isinstance(parsed, str):
        parts = [parsed]
    else:
        parts = raw.replace(",", ",").split(",")
    return [p.strip() for p in parts if p.strip() and p.strip() != "无"]


def resume_summary(item: dict[str, Any]) -> dict[str, Any]:
    """检索列表里的一份简历 → 摘要(不含正文与姓名)。"""
    fields = list(item.get("simpleFields") or [])
    return {
        "resumeId": item.get("resumeId"),
        "userId": item.get("userId"),
        "cycleId": item.get("cycleId"),
        "status": item.get("status"),
        "submittedAt": item.get("submittedAt"),
        "major": _field_value(fields, _MAJOR_KEYS, _MAJOR_LABELS) or None,
        "grade": _field_value(fields, _GRADE_KEYS, _GRADE_LABELS) or None,
        "intendedDepartments": _departments(_field_value(fields, _DEPT_KEYS, _DEPT_LABELS)),
    }


def resume_page(data: Any) -> dict[str, Any]:
    """简历检索响应(分页结构或裸列表)→ {total, items}。"""
    if data is None:
        return {"total": 0, "items": []}
    if isinstance(data, list):
        return {"total": len(data), "items": [resume_summary(i) for i in data]}
    items = list(data.get("content") or [])
    return {
        "total": data.get("totalElements", len(items)),
        "items": [resume_summary(i) for i in items],
    }


def _keep_field(f: dict[str, Any]) -> bool:
    if str(f.get("fieldType") or "").casefold() in _SKIP_FIELD_TYPES:
        return False
    label = str(f.get("fieldLabel") or "")
    if any(hint in label for hint in _SKIP_FIELD_LABEL_HINTS):
        return False
    value = str(f.get("fieldValue") or "")
    return not value.startswith("data:")


def resume_detail(data: Any) -> dict[str, Any]:
    """单份简历详情 → {元信息, fields: {栏目名: 内容}},再整体脱敏。

    字段改成「栏目名 → 内容」的扁平映射:原结构每个字段带 fieldId/fieldType/
    placeholder/sortOrder 一串元数据,对回答无用。以栏目名为键还让键级姓名
    掩码生效(「姓名」在掩码键白名单里)。
    """
    if not isinstance(data, dict):
        return mask_pii_deep(data)
    fields: dict[str, str] = {}
    for f in data.get("simpleFields") or []:
        if not _keep_field(f):
            continue
        label = str(f.get("fieldLabel") or f.get("fieldKey") or "").strip()
        value = str(f.get("fieldValue") or "").strip()
        if label and value:
            fields[label] = value[:_FIELD_VALUE_MAX]
    projected = {
        "resumeId": data.get("resumeId"),
        "userId": data.get("userId"),
        "cycleId": data.get("cycleId"),
        "status": data.get("status"),
        "submittedAt": data.get("submittedAt"),
        "fields": fields,
    }
    return mask_pii_deep(projected)


def open_cycles(data: Any) -> Any:
    """开放周期 → 回答「现在招不招、到什么时候」要用的字段。"""
    if not isinstance(data, list):
        return data
    return [
        {
            "cycleId": c.get("cycleId"),
            "cycleName": c.get("cycleName"),
            "academicYear": c.get("academicYear"),
            "startDate": c.get("startDate"),
            "endDate": c.get("endDate"),
            "intakeOpen": c.get("intakeOpen"),
            "rescheduleOpen": c.get("rescheduleOpen"),
            "description": (str(c.get("description") or "")[:500]) or None,
            "contactInfo": c.get("contactInfo"),
        }
        for c in data
        if isinstance(c, dict)
    ]


def sessions(data: Any) -> Any:
    """可用场次 → 时间/地点/部门/余量。"""
    if not isinstance(data, list):
        return data
    return [
        {
            "sessionId": s.get("sessionId"),
            "date": s.get("interviewDate"),
            "start": s.get("startTime"),
            "end": s.get("endTime"),
            "departments": s.get("deptNames") or ([s["deptName"]] if s.get("deptName") else []),
            "location": s.get("location"),
            "remaining": s.get("remaining"),
            "capacity": s.get("capacity"),
        }
        for s in data
        if isinstance(s, dict)
    ]


def unassigned(data: Any) -> Any:
    """待人工调剂名单 → 简历号 + 志愿 + 原因(姓名不进)。"""
    if not isinstance(data, list):
        return data
    return [
        {
            "resumeId": u.get("resumeId"),
            "userId": u.get("userId"),
            "firstDept": u.get("firstDeptName"),
            "secondDept": u.get("secondDeptName"),
            "reason": u.get("reason"),
        }
        for u in data
        if isinstance(u, dict)
    ]


def reschedule_requests(data: Any) -> Any:
    """改期申请 → 处理所需字段;申请理由是自由文本,过文本脱敏(姓名/学号不进)。"""
    if not isinstance(data, list):
        return data
    return [
        {
            "requestId": r.get("requestId"),
            "requestType": r.get("requestType"),
            "reason": mask_pii(str(r.get("reason") or "")) or None,
            "currentInterviewTime": r.get("currentInterviewTime"),
            "currentLocation": r.get("currentLocation"),
            "preferredSlots": [
                p.get("label") for p in (r.get("preferredSlots") or []) if isinstance(p, dict)
            ],
            "submittedAt": r.get("submittedAt"),
            "status": r.get("status"),
            "awaitingReassign": r.get("awaitingReassign"),
        }
        for r in data
        if isinstance(r, dict)
    ]
