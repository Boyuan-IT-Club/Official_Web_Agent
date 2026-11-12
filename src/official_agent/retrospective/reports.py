"""复盘报告的生成任务:跑编排图 → 渲染 → 落草稿。

生成要调四次模型,耗时以分钟计,所以是后台任务:接口先在报告表里占一个版本
(generating)就返回,这里跑完把正文填进去(draft);任何失败都把该版本标成
failed 并留下原因——不能让它一直停在 generating。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from official_agent.memory import org_store
from official_agent.retrospective import graph
from official_agent.retrospective.render import render_markdown
from official_agent.state.retrospective import report_store

logger = logging.getLogger(__name__)

# 后台任务的强引用集:事件循环只持弱引用,不留在集里的任务可能在完成前被回收
_background_tasks: set[asyncio.Task[None]] = set()


async def generate_report(
    *, cycle_id: int, version: int, snapshot: dict[str, Any], model: Any = None
) -> None:
    """生成一版报告并写回报告表;不抛异常(失败写进该版本的 error)。

    snapshot 是快照正文,要对比基线的话调用方先用 compare.with_baseline 附好差值。
    """
    try:
        report = await graph.run_retrospective(snapshot, model=model)
        prior_lessons = await _prior_lessons(cycle_id)
        markdown = render_markdown(report, snapshot, version=version, prior_lessons=prior_lessons)
        saved = await asyncio.to_thread(
            report_store.finish_report,
            cycle_id,
            version,
            sections={
                **{
                    key: report[key]
                    for key in (
                        "summary",
                        "sections",
                        "lessons",
                        "rubric_candidates",
                        "writer_status",
                    )
                },
                "prior_lessons": prior_lessons,
            },
            report_md=markdown,
            unverified=report["unverified"],
            prompt_versions=_prompt_versions(snapshot),
            usage=report["usage"],
        )
        if not saved:
            # 生成超过了时限,这个版本已被判为失败;迟到的结果按约定丢弃
            logger.warning(
                "复盘报告生成完成但版本已不在生成中,结果未保存 cycle_id=%s version=%s",
                cycle_id,
                version,
            )
            return
        logger.info(
            "复盘报告草稿已生成 cycle_id=%s version=%s writer_status=%s unverified=%d",
            cycle_id,
            version,
            report["writer_status"],
            len(report["unverified"]),
        )
    except Exception as exc:  # noqa: BLE001 — 后台任务的最外层:任何失败都要落成 failed
        logger.warning(
            "复盘报告生成失败 cycle_id=%s version=%s", cycle_id, version, exc_info=True
        )
        await _mark_failed(cycle_id, version, exc)


async def _prior_lessons(cycle_id: int) -> list[dict[str, Any]]:
    """往届留下、还没退役的组织记忆(含已过期的),给这份报告的审阅者复核。

    取不到不影响报告:这一栏是给人的提醒,不是报告的内容(fail-open)。
    """
    try:
        memories = await asyncio.to_thread(org_store.list_from_other_cycles, cycle_id)
    except Exception:  # noqa: BLE001 — 复核清单缺席只是少一栏提醒,报告照常生成
        logger.warning(
            "往届组织记忆读取失败,报告里不列复核清单 cycle_id=%s", cycle_id, exc_info=True
        )
        return []
    return [
        {
            "memory_id": memory.memory_id,
            "kind": memory.kind,
            "content": memory.content,
            "audience": memory.audience,
            "source_cycle": memory.source_cycle,
            "valid_until": memory.valid_until.date().isoformat(),
            "expired": memory.expired,
        }
        for memory in memories
    ]


async def _mark_failed(cycle_id: int, version: int, exc: Exception) -> None:
    """把版本标成 failed;原因只留异常类名,原文在日志里。"""
    try:
        await asyncio.to_thread(
            report_store.fail_report, cycle_id, version, f"生成失败({type(exc).__name__})"
        )
    except Exception:  # noqa: BLE001 — 连失败都记不下来:留给占号时的超时清理兜底
        logger.warning(
            "复盘报告失败状态写入失败,该版本将在超时后被清理 cycle_id=%s version=%s",
            cycle_id,
            version,
            exc_info=True,
        )


def _prompt_versions(snapshot: dict[str, Any]) -> dict[str, Any]:
    """这一版用到的全部版本锚:四份 prompt、指标定义、模型名。"""
    from official_agent.config import get_effective_settings

    return {
        **graph.prompt_versions(),
        "metrics_schema": snapshot.get("schema"),
        "model": get_effective_settings().model_strong,
    }


def spawn_generation(*, cycle_id: int, version: int, snapshot: dict[str, Any]) -> None:
    """在事件循环里起后台生成任务并立即返回。"""
    task = asyncio.create_task(
        generate_report(cycle_id=cycle_id, version=version, snapshot=snapshot)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
