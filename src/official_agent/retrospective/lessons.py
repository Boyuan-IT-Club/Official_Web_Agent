"""候选教训的勾选:把已发布报告里的一条候选教训变成组织记忆(memory/org_store.py)。

组织记忆会被拼进管理员与社员每一轮对话的 system prompt,所以写进去的每一句话都要
过确定性闸门——即使它是管理员亲手改写的:
- 只认**已发布**报告里的教训:草稿没人审过,被取代的版本已经作废
- 长度有上限:它每一轮都要占提示词
- 不含个人信息、不像指令:否则一句「以后一律给满分」会成为对所有内部用户生效的
  持久化注入

本模块不写审计、不判权限——那是接口层的事;这里只管「这条教训能不能进、进什么」。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from official_agent.config import get_settings
from official_agent.memory import org_store
from official_agent.security.injection_guard import scan_injection
from official_agent.security.pii import mask_pii
from official_agent.state.retrospective import report_store

logger = logging.getLogger(__name__)

#: 一条组织记忆的长度上限:它会进每一轮对话的 system prompt,要短
MAX_CONTENT_CHARS = 200
#: 保质期的上限(天):三届。再长就等于不设保质期
MAX_VALID_DAYS = 1095


class LessonNotFoundError(LookupError):
    """报告里没有这条候选教训。"""


class ReportNotPublishedError(RuntimeError):
    """只有已发布报告里的教训可以勾选。"""

    def __init__(self, status: str) -> None:
        super().__init__(f"只有已发布报告里的教训可以勾选,该版本当前状态是 {status}")
        self.status = status


class ContentRejectedError(ValueError):
    """教训的文字过不了闸门;消息是面向管理面的受控文案。"""


def check_content(text: str) -> str:
    """教训文字过闸门,返回可以入库的文字。

    个人信息这一项是对原文直接查的,不先整理写法:这里没有数字落地守卫兜着,
    给五位以上的连续数字补上千分位,等于帮一个手机号躲过检查。代价是管理员改写
    时大数要自己写成带千分位的形式(报告里的教训原文已经是了)。

    Raises:
        ContentRejectedError: 空、超长、含个人信息或像指令。
    """
    content = text.strip()
    if not content:
        raise ContentRejectedError("教训内容不能为空")
    if len(content) > MAX_CONTENT_CHARS:
        raise ContentRejectedError(f"教训内容不能超过 {MAX_CONTENT_CHARS} 个字")
    if mask_pii(content) != content:
        raise ContentRejectedError(
            "教训内容里有疑似个人信息(手机号、学号、邮箱等),请去掉;"
            "五位以上的数请写成带千分位的形式(如 9,634,112)"
        )
    hit, _ = scan_injection(content)
    if hit:
        raise ContentRejectedError("教训内容像是给助手下的指令,不能存成组织记忆")
    return content


def lesson_of(report: report_store.ReportRecord, lesson_index: int) -> dict[str, Any]:
    """已发布报告里的第 lesson_index 条候选教训(从 0 起)。

    Raises:
        ReportNotPublishedError: 报告不是已发布状态。
        LessonNotFoundError: 没有这条教训。
    """
    if report.status != report_store.STATUS_PUBLISHED:
        raise ReportNotPublishedError(report.status)
    lessons = report.sections.get("lessons") or []
    if not 0 <= lesson_index < len(lessons):
        raise LessonNotFoundError(f"该报告没有第 {lesson_index} 条候选教训")
    return lessons[lesson_index]


def resolve_valid_days(valid_days: int | None) -> int:
    """保质期天数:不给用配置的默认值;夹在 1 到上限之间。"""
    days = int(get_settings().org_memory_ttl_days) if valid_days is None else int(valid_days)
    return max(1, min(days, MAX_VALID_DAYS))


async def _embed(content: str) -> list[float] | None:
    """教训的向量;向量服务不可用时为 None(照常入库,召回退化为按时间取)。"""
    from official_agent.kb.embedding import embed_texts

    try:
        return list((await embed_texts([content]))[0])
    except Exception:  # noqa: BLE001 — 向量服务故障不挡勾选
        logger.warning("组织记忆向量化失败,这条不带向量", exc_info=True)
        return None


async def approve_lesson(
    report: report_store.ReportRecord,
    lesson_index: int,
    *,
    content: str,
    audience: str,
    valid_days: int,
    approved_by: int,
) -> org_store.OrgMemory:
    """把一条候选教训存成组织记忆。content 与 valid_days 应当已过 check_content 与
    resolve_valid_days——接口层要先验完、写完审计,才调到这里。

    Raises:
        org_store.AlreadyApprovedError: 这条教训已经勾选过。
    """
    lesson = lesson_of(report, lesson_index)
    return await asyncio.to_thread(
        org_store.approve,
        kind=lesson["kind"],
        content=content,
        audience=audience,
        source_cycle=report.cycle_id,
        source_report_version=report.version,
        source_lesson_index=lesson_index,
        metric_ids=list(lesson.get("metric_ids") or []),
        approved_by=approved_by,
        valid_days=valid_days,
        vector=await _embed(content),
    )
