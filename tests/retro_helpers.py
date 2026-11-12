"""周期复盘测试的公共件:合成快照与可编排的假模型。

合成快照经真实的口径与抑制代码拼出来(metrics.snapshot_from_aggregates),只有取数
被替换;假模型按 prompt 里的角色标记决定回什么,并记下每次调用。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from official_agent.retrospective import metrics
from official_agent.retrospective.compare import with_baseline


def topic_row(
    rank: int,
    topic: str | None,
    *,
    queries: int,
    threads: int,
    near_miss: int = 0,
    scope: str | None = "official_knowledge",
    triage_status: str = "done",
) -> dict[str, Any]:
    """知识库离线分析的一个主题行(形状同 state.kb_ops.list_topics 的返回,只留用得到的列)。"""
    return {
        "rank": rank,
        "topic": topic,
        "scope": scope,
        "triage_status": triage_status,
        "query_count": queries,
        "thread_count": threads,
        "near_miss_count": near_miss,
    }


NORMAL_AGGREGATES: dict[str, Any] = {
    "funnel": {
        "totalResumes": 213,
        "totalResults": 180,
        "decisionCounts": {"pending": 12, "passed": 68, "rejected": 92, "toTransfer": 8},
        "assignedByDeptId": {"1": 52, "2": 44, "3": 41, "4": 37},
        "evaluatedCandidates": 150,
    },
    "conversation": {
        "turns": 4000,
        "sessions": 900,
        "error_turns": 80,
        "timed_turns": 3900,
        "duration_p50_ms": 1835,
        "duration_p90_ms": 5120,
        "duration_p99_ms": 20480,
        "metered_turns": 3800,
        "input_tokens": 9_634_112,
        "output_tokens": 600_000,
        "cache_turns": 3700,
        "cache_hit_tokens": 6_000_000,
        "cache_miss_tokens": 2_000_000,
        "by_channel": {"web": 3900, "cli": 100},
        "tool_calls": {"search_knowledge": 2500, "get_open_cycle": 800},
    },
    "kb": {
        "searches": 2500,
        "hits": 2000,
        "not_found": 450,
        "unavailable": 50,
        "low_score_hits": 300,
    },
    "screening": {
        "jobs": 230,
        "failed_jobs": 12,
        "by_attempts": {1: 205, 2: 18, 3: 7},
        "scored_resumes": 200,
        "hard_zero_resumes": 30,
        "card_versions": 215,
        "decisions": {
            ("adopted", "技术部"): 34,
            ("adopted", "项目部"): 28,
            ("adopted", "媒体部"): 26,
            ("adopted", "综合部"): 24,
            ("rejected", "技术部"): 25,
            ("rejected", "项目部"): 2,
            ("rejected", "媒体部"): 9,
            ("rejected", "综合部"): 8,
        },
    },
    "qbank": {
        "generated": 190,
        "degraded": 19,
        "picked_resumes": 80,
        "offered_by_group": {"repo": 600, "awards": 100},
        "picked_by_group": {"repo": 240, "awards": 20},
    },
    "evaluation_usage": {
        "metered_resumes": 150,
        "input_tokens": 30_000_000,
        "output_tokens": 1_500_000,
        "scoring_metered_resumes": 200,
        "scoring_input_tokens": 2_400_000,
        "scoring_output_tokens": 300_000,
    },
    "agreement": {
        "graded_decided": 141,
        "low_confidence_decided": 40,
        "low_confidence_rejected": 9,
    },
    "kb_topics": {
        "run": {
            "run_id": "kbops-test",
            "window_start": "2026-11-01T00:00:00+08:00",
            "window_end": "2026-11-30T00:00:00+08:00",
        },
        "topics": [
            topic_row(1, "招新报名截止时间", queries=120, threads=85, near_miss=20),
            topic_row(2, "面试地点与形式", queries=64, threads=50, near_miss=40),
            topic_row(3, "查询本人面试结果", queries=55, threads=41, scope="personal_data"),
            topic_row(4, "各部门日常工作", queries=30, threads=22, near_miss=3),
        ],
    },
}


async def make_snapshot(
    *,
    cycle_id: int = 12,
    without: tuple[str, ...] = (),
    baseline: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """一份合成快照。overrides 按域整体替换聚合;without 去掉某些域;baseline 给了就附差值。"""
    aggregates = copy.deepcopy(NORMAL_AGGREGATES)
    aggregates.update(overrides)
    for domain in without:
        aggregates.pop(domain, None)
    window = metrics.cycle_window(_date(2026, 9, 1), _date(2026, 11, 30))
    snapshot = await metrics.snapshot_from_aggregates(cycle_id, aggregates, window=window)
    if baseline is not None:
        snapshot = with_baseline(snapshot, baseline)
    return snapshot


def _date(year: int, month: int, day: int):
    from datetime import date

    return date(year, month, day)


ROLE_MARKERS = {
    "data": "**数据分析员**",
    "qa": "**问答分析员**",
    "evaluation": "**评估分析员**",
    "writer": "**撰稿人**",
}


class _Reply:
    def __init__(self, content: str, usage: dict[str, int] | None) -> None:
        self.content = content
        self.response_metadata = {"token_usage": usage} if usage else {}


class ScriptedModel:
    """按角色回放预设回复的假模型。

    script: 角色 → 回复,或回复的列表(按调用次序取,取完后重复最后一个)。回复是
    dict(序列化成 JSON)、str(原样返回)或异常(抛出)。没写的角色回空结果。
    calls 记下每次调用的 (角色, prompt 全文)。
    """

    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.script = {role: _as_list(reply) for role, reply in (script or {}).items()}
        self.calls: list[tuple[str, str]] = []

    def roles_called(self) -> list[str]:
        return [role for role, _ in self.calls]

    def prompt_for(self, role: str, index: int = 0) -> str:
        return [prompt for called, prompt in self.calls if called == role][index]

    async def ainvoke(self, messages: Any, config: Any = None) -> _Reply:
        prompt = messages[0].content
        role = next(r for r, marker in ROLE_MARKERS.items() if marker in prompt)
        seen = sum(1 for called, _ in self.calls if called == role)
        self.calls.append((role, prompt))
        replies = self.script.get(role)
        if not replies:
            reply: Any = {"findings": []} if role != "writer" else {}
        else:
            reply = replies[min(seen, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return _Reply(content, {"prompt_tokens": 100, "completion_tokens": 10})


def _as_list(reply: Any) -> list[Any]:
    return list(reply) if isinstance(reply, list) else [reply]


def finding(statement: str, *metric_ids: str, causes: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"statement": statement, "metric_ids": list(metric_ids), "causes": list(causes)}


def item(text: str, *metric_ids: str) -> dict[str, Any]:
    return {"text": text, "metric_ids": list(metric_ids)}
