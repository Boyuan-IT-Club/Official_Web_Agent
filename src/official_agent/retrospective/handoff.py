"""报告里由代码直接放进去的交接栏目:指给别的环节去处理的清单。纯函数,零 IO。

复盘只负责把问题指出来,不替别的环节干活(docs/proposals/cycle-retrospective.md D7):
候选人反复问、知识库没答上的主题,列出来交给知识库运营的草稿通道去补,复盘不起草。

栏目的内容全部取自指标快照——过了小格子抑制的数、过了清单规则的文字——所以这里
不读库、不调模型:同一份快照永远出同样的栏目,模型也没有机会改写其中的任何一项。

上游没有数据时栏目照常出,状态里写明原因;一个栏目没有内容不该让整份报告失败。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from official_agent.retrospective.catalog import STATUS_OK as METRIC_AVAILABLE

#: 知识库缺口清单取自这个指标
KB_GAPS_METRIC = "kb.top_topics"

STATUS_OK = "ok"  # 有主题可列
STATUS_EMPTY = "empty"  # 上游有数据,但没有一个主题够格进快照
STATUS_ABSENT = "absent"  # 上游没有数据,原因在 reason 里

_NO_TOPIC_QUALIFIES = "知识库离线分析里该由知识库回答的主题,检索次数或会话数都不足以列出"
_GAP_LABELS = {"missing": "知识库里多半没有", "wording": "多半有,问法没对上"}


def kb_gap_list(snapshot: dict[str, Any]) -> dict[str, Any]:
    """知识库缺口清单:快照里的高频主题整理成给人看的一栏。

    返回 {status, reason, window, items}:
    - items 每项是 {metric_id, topic, searches, gap}——主题名、检索次数、缺口类型
      (missing / wording / None),按次数从大到小
    - window 是这些次数所在的时间窗 {start, end}(知识库离线分析的时间窗,不是周期的)
    - status 不是 ok 时 items 为空,reason 说明为什么
    """
    items = [
        {
            "metric_id": metric["id"],
            "topic": metric["label"],
            "searches": metric["value"],
            "gap": metric.get("group"),
        }
        for metric in snapshot.get("metrics", [])
        if metric.get("metric") == KB_GAPS_METRIC
        and metric.get("label")
        and not metric.get("suppressed")
        and metric.get("value") is not None
    ]
    items.sort(key=lambda item: -item["searches"])
    if items:
        return {"status": STATUS_OK, "reason": None, "window": _window(snapshot), "items": items}
    reason = _missing_reason(snapshot)
    if reason is None:
        return {
            "status": STATUS_EMPTY,
            "reason": _NO_TOPIC_QUALIFIES,
            "window": _window(snapshot),
            "items": [],
        }
    return {"status": STATUS_ABSENT, "reason": reason, "window": None, "items": []}


def _missing_reason(snapshot: dict[str, Any]) -> str | None:
    """快照里为什么没有高频主题;上游其实有数据(只是没有主题够格)时返回 None。"""
    for absent in snapshot.get("absent", []):
        if absent.get("id") == KB_GAPS_METRIC:
            return str(absent.get("reason") or "这次采集没有取到")
    entry = next(
        (e for e in snapshot.get("catalog", []) if e.get("id") == KB_GAPS_METRIC), None
    )
    if entry is None:
        return "这份快照的指标定义里没有高频主题"
    if entry.get("status") != METRIC_AVAILABLE:
        return str(entry.get("reason") or "该指标暂缺")
    domain = (snapshot.get("domains") or {}).get(entry.get("domain")) or {}
    if domain.get("status") != METRIC_AVAILABLE:
        return str(domain.get("reason") or "知识库域这次没有数据")
    return None


def _window(snapshot: dict[str, Any]) -> dict[str, str] | None:
    params = snapshot.get("params") or {}
    start, end = params.get("kb_topics_window_start"), params.get("kb_topics_window_end")
    if not start or not end:
        return None
    return {"start": str(start), "end": str(end)}


def gap_label(gap: str | None) -> str:
    """缺口类型写成给人看的话;没有归类写成一横。"""
    return _GAP_LABELS.get(gap or "", "—")


def window_text(window: dict[str, str] | None) -> str:
    """时间窗写成「某日至某日」;没有记录时为空串。"""
    if not window:
        return ""
    return f"{_day(window['start'])} 至 {_day(window['end'])}"


def _day(moment: str) -> str:
    try:
        return datetime.fromisoformat(moment).date().isoformat()
    except ValueError:
        return moment
