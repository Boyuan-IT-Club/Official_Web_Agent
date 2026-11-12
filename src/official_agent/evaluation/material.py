"""简历材料进 prompt 的统一排布:报名信息 + 简历全文,全部包数据区。

评委、批判者、答辩看到的简历文本都由这里排出来。同一份简历排出的文本逐字节相同,
模型侧的前缀缓存才能命中;「简历是不可信输入,原文一律包数据区」也只在这一处落实。
"""

from __future__ import annotations

from typing import Any

from official_agent.evaluation.applicant import ApplicantProfile
from official_agent.security.injection_guard import wrap_data_zone


def resume_material(fields: list[dict[str, Any]], profile: ApplicantProfile) -> str:
    """报名信息与简历各栏 → 一段 prompt 文本。fields 元素:{field_key, title, value}。

    调用方负责保证 value 已脱敏;这里只做排布与包数据区。
    """
    applicant = wrap_data_zone(
        "applicant",
        f"第一志愿:{profile.first_dept or '未填'}\n"
        f"第二志愿:{profile.second_dept or '未填'}\n"
        f"专业:{profile.major or '未填'}\n"
        f"年级:{profile.grade or '未填'}",
    )
    blocks = [
        "### "
        + (f.get("title") or f["field_key"])
        + f" (field_key={f['field_key']})\n"
        + wrap_data_zone(f"resume:{f['field_key']}", str(f.get("value", "")))
        for f in fields
    ]
    return "报名信息:\n\n" + applicant + "\n\n简历全文:\n\n" + "\n\n".join(blocks)


def resume_sources(fields: list[dict[str, Any]]) -> list[str]:
    """简历各栏的原文,引文落地校验拿它作对照。报名信息不算原文,不能当引文。"""
    return [str(f.get("value", "")) for f in fields]
