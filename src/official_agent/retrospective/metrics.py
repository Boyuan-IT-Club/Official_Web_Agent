"""各域口径与 collect_metrics:把一个招新周期的各数据域算成一份指标快照。

- 各域的 *_metrics 是纯函数:原始聚合 → Raw* 指标。算法写在这里,每个指标的名字与
  口径说明在 retrospective/catalog.py;两边的 id 必须对得上(collect_metrics 会核对)
- collect_metrics 负责取数与编排:某个数据源不可用,只有用到它的域缺席并在快照里
  标注,其余照常;原始聚合一律过 guard.suppress 才进快照

数字全部由代码算,这里没有模型调用。对后端只读,并且用服务账号读——快照是跨周期
对比的基线,内容不该随触发人的后端权限而变,所以调用方不要把它包进来问者作用域
(tools.readonly.asker_scope)。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg

from official_agent.config import get_settings
from official_agent.evaluation.schema import DEPARTMENTS
from official_agent.retrospective.catalog import (
    CATALOG,
    DOMAIN_CONVERSATION,
    DOMAIN_COST,
    DOMAIN_FUNNEL,
    DOMAIN_KB,
    DOMAIN_QBANK,
    DOMAIN_SCREENING,
    METRICS_SCHEMA,
    STATUS_OK,
    STATUS_PENDING,
    STATUS_UNAVAILABLE,
    MetricSpec,
    catalog_payload,
)
from official_agent.retrospective.guard import (
    SMALL_CELL_MIN,
    UNIT_MS,
    UNIT_RATIO,
    UNIT_TOKENS,
    MetricValue,
    RawBreakdown,
    RawCount,
    RawMetric,
    RawRatio,
    RawRatioBreakdown,
    RawStat,
    suppress,
)
from official_agent.state import retrospective as store
from official_agent.tools import readonly
from official_agent.tools.client import (
    BackendAuthError,
    BackendError,
    BackendUnavailableError,
)

logger = logging.getLogger(__name__)

# 后端按北京时间判断「今天是否落在周期内」。中国不实行夏令时,固定偏移与时区库
# 等价,且不依赖运行镜像里装了 tzdata(slim 镜像没有)。
CYCLE_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")

# 评分卡上没有志愿部门的简历归到这一格(没填,或旧版卡不带这项)
_NO_DEPT_KEY = "未填志愿"
# 按部门拆的分布只认这些键:评分卡上的志愿部门已归一到标准部门名
_DEPT_KEYS = frozenset({*DEPARTMENTS, _NO_DEPT_KEY})
# 每个标准部门的格子固定出:某部门没出现就等于明说那里是 0 人
_FIXED_DEPT_KEYS = frozenset(DEPARTMENTS)
# 分布里固定出的判定结果(0 也出格子并照样被抑制);其余的格子是流程状态,为 0 不出
_FUNNEL_OUTCOMES = frozenset({"passed", "rejected"})
_SCREENING_OUTCOMES = frozenset({"adopted", "rejected"})
# 后端面试结果里还没分配部门的归到这一格
_UNASSIGNED_KEY = "unassigned"


@dataclass(frozen=True)
class Absent:
    """目录里有、但这次采集取不到值的指标,带原因。"""

    id: str
    reason: str


_SPECS: dict[str, MetricSpec] = {spec.id: spec for spec in CATALOG}


class CycleNotFoundError(LookupError):
    """后端明确答复查不到这个周期:不采集、不落快照。"""

    def __init__(self, cycle_id: int) -> None:
        super().__init__(f"后端查不到周期 {cycle_id},未采集")
        self.cycle_id = cycle_id


class CycleWindowError(ValueError):
    """周期在,但起止日期缺失或不成区间;消息是面向管理面的受控文案。"""


class SourceUnavailableError(RuntimeError):
    """某个数据源这次没有数据;消息是写进快照的受控文案。"""


@dataclass(frozen=True)
class CycleWindow:
    """周期的时间窗,半开区间 [start, end)。"""

    start: datetime
    end: datetime


def cycle_window(start_date: date, end_date: date) -> CycleWindow:
    """周期的起止日期(首尾两天都算在内)→ 北京时间的半开时间窗。"""
    if end_date < start_date:
        raise CycleWindowError(f"周期的结束日期 {end_date} 早于开始日期 {start_date}")
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=CYCLE_TIMEZONE)
    end = datetime.combine(
        end_date + timedelta(days=1), datetime.min.time(), tzinfo=CYCLE_TIMEZONE
    )
    return CycleWindow(start=start, end=end)


def _parse_backend_date(value: Any, field: str) -> date:
    """后端的日期字段 → date。ISO 字符串与 [年, 月, 日] 数组两种序列化都认。"""
    try:
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
        if isinstance(value, list | tuple) and len(value) >= 3:
            return date(int(value[0]), int(value[1]), int(value[2]))
    except (TypeError, ValueError):
        pass  # 落到下面统一报错
    raise CycleWindowError(f"后端返回的周期缺少可用的 {field}")


async def fetch_cycle_window(cycle_id: int) -> CycleWindow:
    """向后端要周期的起止日期,换成时间窗(GET /api/cycles/{cycleId})。

    Raises:
        CycleNotFoundError: 后端以业务错误答复(周期不存在)。
        BackendUnavailableError / BackendAuthError: 后端不可达或服务账号登录失败。
        CycleWindowError: 周期在,但起止日期缺失或不成区间。
    """
    client = await readonly.get_backend_client()
    try:
        data = await client.get(f"/api/cycles/{cycle_id}")
    except (BackendUnavailableError, BackendAuthError):
        raise
    except BackendError as exc:
        logger.warning("复盘指标采集:后端拒绝了周期查询 cycle_id=%s", cycle_id, exc_info=True)
        raise CycleNotFoundError(cycle_id) from exc
    if not isinstance(data, dict):
        raise CycleWindowError("后端没有返回周期信息")
    return cycle_window(
        _parse_backend_date(data.get("startDate"), "startDate"),
        _parse_backend_date(data.get("endDate"), "endDate"),
    )


# ── 各域口径:原始聚合 → 指标(纯函数) ──────────────────────────────


def funnel_metrics(stats: Mapping[str, Any]) -> list[RawMetric | Absent]:
    """漏斗域。stats 是 tools.readonly.get_recruit_statistics 的返回。"""
    out: list[RawMetric | Absent] = []
    total_resumes = stats.get("totalResumes")
    if total_resumes is None:
        out.append(Absent("funnel.resumes_total", "后端简历检索没有返回总条数"))
    else:
        out.append(RawCount("funnel.resumes_total", int(total_resumes)))
    results = int(stats.get("totalResults") or 0)
    out.append(RawCount("funnel.results_total", results))
    out.append(
        RawBreakdown(
            "funnel.decisions",
            dict(stats.get("decisionCounts") or {}),
            fixed_keys=_FUNNEL_OUTCOMES,
        )
    )
    by_dept = {str(k): int(v) for k, v in (stats.get("assignedByDeptId") or {}).items()}
    # 没分配的也要占一格:否则结果总数减去各部门之和就是它,人少时照样能减出来
    by_dept[_UNASSIGNED_KEY] = max(results - sum(by_dept.values()), 0)
    out.append(RawBreakdown("funnel.assigned_by_dept", by_dept))
    evaluated = stats.get("evaluatedCandidates")
    if evaluated is None:
        out.append(Absent("funnel.evaluated_candidates", "该周期没开评价表,后端没有评价汇总"))
    else:
        out.append(RawCount("funnel.evaluated_candidates", int(evaluated)))
    return out


def conversation_metrics(agg: Mapping[str, Any]) -> list[RawMetric | Absent]:
    """对话域。agg 是 state.retrospective.conversation_aggregates 的返回。"""
    turns = int(agg["turns"])
    timed = int(agg["timed_turns"])
    metered = int(agg["metered_turns"])
    cache_total = int(agg["cache_hit_tokens"]) + int(agg["cache_miss_tokens"])
    return [
        RawCount("conversation.turns", turns),
        RawCount("conversation.sessions", int(agg["sessions"])),
        RawBreakdown("conversation.turns_by_channel", agg["by_channel"]),
        RawRatio("conversation.error_rate", int(agg["error_turns"]), turns),
        RawStat("conversation.duration_p50_ms", _whole(agg["duration_p50_ms"]), timed, UNIT_MS),
        RawStat("conversation.duration_p90_ms", _whole(agg["duration_p90_ms"]), timed, UNIT_MS),
        RawStat("conversation.duration_p99_ms", _whole(agg["duration_p99_ms"]), timed, UNIT_MS),
        RawBreakdown("conversation.tool_calls", agg["tool_calls"]),
        RawStat("conversation.input_tokens", int(agg["input_tokens"]), metered, UNIT_TOKENS),
        RawStat("conversation.output_tokens", int(agg["output_tokens"]), metered, UNIT_TOKENS),
        RawStat(
            "conversation.cache_hit_rate",
            round(int(agg["cache_hit_tokens"]) / cache_total, 4) if cache_total else None,
            int(agg["cache_turns"]),
            UNIT_RATIO,
        ),
    ]


def kb_metrics(agg: Mapping[str, int]) -> list[RawMetric | Absent]:
    """知识库域。agg 是 state.retrospective.kb_aggregates 的返回。"""
    searches = int(agg["searches"])
    return [
        RawCount("kb.searches", searches),
        RawRatio("kb.not_found_rate", int(agg["not_found"]), searches),
        RawRatio("kb.unavailable_rate", int(agg["unavailable"]), searches),
        RawRatio("kb.low_score_hit_rate", int(agg["low_score_hits"]), int(agg["hits"])),
    ]


def screening_metrics(agg: Mapping[str, Any]) -> list[RawMetric | Absent]:
    """初筛域。agg 是 state.retrospective.screening_aggregates 的返回。"""
    jobs = int(agg["jobs"])
    scored = int(agg["scored_resumes"])
    by_status: dict[str, dict[str, int]] = {"adopted": {}, "rejected": {}}
    for (status, first_dept), resumes in agg["decisions"].items():
        cells = by_status[status]
        dept = first_dept or _NO_DEPT_KEY
        cells[dept] = cells.get(dept, 0) + int(resumes)
    adopted = sum(by_status["adopted"].values())
    rejected = sum(by_status["rejected"].values())
    return [
        RawCount("screening.jobs", jobs),
        RawRatio("screening.job_failure_rate", int(agg["failed_jobs"]), jobs),
        RawBreakdown(
            "screening.jobs_by_attempts", {str(k): v for k, v in agg["by_attempts"].items()}
        ),
        RawCount("screening.scored_resumes", scored),
        RawRatio("screening.hard_zero_rate", int(agg["hard_zero_resumes"]), scored),
        RawCount("screening.rescore_count", int(agg["card_versions"]) - scored),
        RawBreakdown(
            "screening.decisions",
            {"adopted": adopted, "rejected": rejected, "undecided": scored - adopted - rejected},
            fixed_keys=_SCREENING_OUTCOMES,
        ),
        RawBreakdown(
            "screening.adopted_by_dept", by_status["adopted"], _DEPT_KEYS, _FIXED_DEPT_KEYS
        ),
        RawBreakdown(
            "screening.rejected_by_dept", by_status["rejected"], _DEPT_KEYS, _FIXED_DEPT_KEYS
        ),
    ]


def qbank_metrics(agg: Mapping[str, Any]) -> list[RawMetric | Absent]:
    """出题域。agg 是 state.retrospective.qbank_aggregates 的返回。"""
    generated = int(agg["generated"])
    picked = agg["picked_by_group"]
    return [
        RawCount("qbank.generated", generated),
        RawRatio("qbank.degraded_rate", int(agg["degraded"]), generated),
        RawCount("qbank.picked_resumes", int(agg["picked_resumes"])),
        RawRatioBreakdown(
            "qbank.pick_rate_by_group",
            {
                kind: (int(picked.get(kind, 0)), int(offered))
                for kind, offered in agg["offered_by_group"].items()
            },
        ),
    ]


def cost_metrics(
    conversation: Mapping[str, Any], usage: Mapping[str, int]
) -> list[RawMetric | Absent]:
    """成本域。conversation 是对话聚合,usage 是 evaluation_usage_aggregates 的返回。"""
    evaluation_tokens = int(usage["input_tokens"]) + int(usage["output_tokens"])
    resumes = int(usage["metered_resumes"])
    return [
        RawStat(
            "cost.conversation_tokens",
            int(conversation["input_tokens"]) + int(conversation["output_tokens"]),
            int(conversation["metered_turns"]),
            UNIT_TOKENS,
        ),
        RawStat("cost.evaluation_tokens", evaluation_tokens, resumes, UNIT_TOKENS),
        RawStat(
            "cost.evaluation_tokens_per_resume",
            round(evaluation_tokens / resumes) if resumes else None,
            resumes,
            UNIT_TOKENS,
        ),
    ]


def _whole(value: float | None) -> int | None:
    return None if value is None else round(float(value))


# ── 取数与编排 ───────────────────────────────────────────────


class _Sources:
    """一次采集里的取数入口:每个数据源至多取一次,失败也记住。

    几个域共用一个源时(对话聚合同时喂对话域与成本域,时间窗喂三个域),后用到的
    域拿到同一个结果或同一个异常,不重复查询。
    """

    def __init__(self, cycle_id: int, *, kb_min_score: float) -> None:
        self.cycle_id = cycle_id
        self.kb_min_score = kb_min_score
        self._values: dict[str, Any] = {}
        self._errors: dict[str, Exception] = {}

    async def _once(self, name: str, load: Callable[[], Awaitable[Any]]) -> Any:
        if name in self._errors:
            raise self._errors[name]
        if name not in self._values:
            try:
                self._values[name] = await load()
            except Exception as exc:
                self._errors[name] = exc
                raise
        return self._values[name]

    async def window(self) -> CycleWindow:
        return await self._once("window", lambda: fetch_cycle_window(self.cycle_id))

    async def funnel(self) -> dict[str, Any]:
        return await self._once(
            "funnel", lambda: readonly.get_recruit_statistics(self.cycle_id)
        )

    async def conversation(self) -> dict[str, Any]:
        window = await self.window()
        return await self._once(
            "conversation",
            lambda: asyncio.to_thread(store.conversation_aggregates, window.start, window.end),
        )

    async def kb(self) -> dict[str, int]:
        window = await self.window()
        return await self._once(
            "kb",
            lambda: asyncio.to_thread(
                store.kb_aggregates, window.start, window.end, min_score=self.kb_min_score
            ),
        )

    async def screening(self) -> dict[str, Any]:
        return await self._once(
            "screening", lambda: asyncio.to_thread(store.screening_aggregates, self.cycle_id)
        )

    async def qbank(self) -> dict[str, Any]:
        return await self._once(
            "qbank", lambda: asyncio.to_thread(store.qbank_aggregates, self.cycle_id)
        )

    async def evaluation_usage(self) -> dict[str, int]:
        return await self._once(
            "evaluation_usage",
            lambda: asyncio.to_thread(store.evaluation_usage_aggregates, self.cycle_id),
        )


async def _collect_funnel(src: _Sources) -> list[RawMetric | Absent]:
    return funnel_metrics(await src.funnel())


async def _collect_conversation(src: _Sources) -> list[RawMetric | Absent]:
    return conversation_metrics(await src.conversation())


async def _collect_kb(src: _Sources) -> list[RawMetric | Absent]:
    return kb_metrics(await src.kb())


async def _collect_screening(src: _Sources) -> list[RawMetric | Absent]:
    return screening_metrics(await src.screening())


async def _collect_qbank(src: _Sources) -> list[RawMetric | Absent]:
    return qbank_metrics(await src.qbank())


async def _collect_cost(src: _Sources) -> list[RawMetric | Absent]:
    return cost_metrics(await src.conversation(), await src.evaluation_usage())


_COLLECTORS: tuple[
    tuple[str, Callable[[_Sources], Awaitable[list[RawMetric | Absent]]]], ...
] = (
    (DOMAIN_FUNNEL, _collect_funnel),
    (DOMAIN_CONVERSATION, _collect_conversation),
    (DOMAIN_KB, _collect_kb),
    (DOMAIN_SCREENING, _collect_screening),
    (DOMAIN_QBANK, _collect_qbank),
    (DOMAIN_COST, _collect_cost),
)

class _CannedSources(_Sources):
    """拿现成的原始聚合充当数据源,不碰后端与数据库。

    eval 与测试用它造合成快照:口径、抑制、快照结构都走真实代码,只有取数被替换。
    没给的域按数据源不可用处理。
    """

    def __init__(
        self,
        cycle_id: int,
        aggregates: Mapping[str, Any],
        *,
        window: CycleWindow | None,
        kb_min_score: float,
    ) -> None:
        super().__init__(cycle_id, kb_min_score=kb_min_score)
        self._aggregates = aggregates
        self._window = window

    def _given(self, name: str) -> Any:
        if name not in self._aggregates:
            raise SourceUnavailableError("合成快照没有提供这个域的数据")
        return self._aggregates[name]

    async def window(self) -> CycleWindow:
        if self._window is None:
            raise SourceUnavailableError("合成快照没有给周期时间窗")
        return self._window

    async def funnel(self) -> dict[str, Any]:
        return self._given("funnel")

    async def conversation(self) -> dict[str, Any]:
        return self._given("conversation")

    async def kb(self) -> dict[str, int]:
        return self._given("kb")

    async def screening(self) -> dict[str, Any]:
        return self._given("screening")

    async def qbank(self) -> dict[str, Any]:
        return self._given("qbank")

    async def evaluation_usage(self) -> dict[str, int]:
        return self._given("evaluation_usage")


# 数据源不可用的几种形态。只认这些:口径函数里的程序错误不该被当成「数据源
# 不可用」吞掉,要照常抛出去。
_SOURCE_ERRORS = (BackendError, psycopg.Error, CycleWindowError, SourceUnavailableError)


def _unavailable_reason(exc: Exception) -> str:
    """数据源失败 → 写进快照的原因。只给类别与异常类名,异常原文留在日志里。"""
    if isinstance(exc, SourceUnavailableError):
        return str(exc)
    if isinstance(exc, CycleWindowError):
        return f"周期时间窗取不到:{exc}"
    if isinstance(exc, BackendError):
        return f"后端接口不可用({type(exc).__name__})"
    return f"agent 数据库查询失败({type(exc).__name__})"


async def collect_metrics(cycle_id: int) -> dict[str, Any]:
    """算出一个周期的指标快照正文(可直接存进 cycle_metrics_snapshot)。

    某个数据源不可用时,用到它的域在 domains 里标 unavailable 并带原因,该域的
    指标缺席,其余域照常——不因为一个源整体失败。

    Raises:
        CycleNotFoundError: 后端查不到这个周期。
    """
    return await _collect(_Sources(cycle_id, kb_min_score=float(get_settings().kb_min_score)))


async def snapshot_from_aggregates(
    cycle_id: int,
    aggregates: Mapping[str, Any],
    *,
    window: CycleWindow | None = None,
    kb_min_score: float = 0.45,
) -> dict[str, Any]:
    """用现成的原始聚合拼一份快照正文,不碰后端与数据库(eval 与测试造合成快照用)。

    aggregates 的键是 funnel / conversation / kb / screening / qbank /
    evaluation_usage,值的形状与对应的取数函数返回一致;没给的域标 unavailable。
    """
    return await _collect(
        _CannedSources(cycle_id, aggregates, window=window, kb_min_score=kb_min_score)
    )


async def _collect(src: _Sources) -> dict[str, Any]:
    """逐域取数、过口径与抑制,拼成快照正文。"""
    cycle_id, kb_min_score = src.cycle_id, src.kb_min_score
    # 先取时间窗:周期不存在在这里就抛出去,不会走到任何一个域
    try:
        window: CycleWindow | None = await src.window()
    except _SOURCE_ERRORS:
        window = None  # 原因由下面用到时间窗的域各自标注

    domains: dict[str, dict[str, str]] = {}
    values: list[MetricValue] = []
    absent: list[Absent] = []
    for domain, collect in _COLLECTORS:
        try:
            produced = await collect(src)
        except _SOURCE_ERRORS as exc:
            # 降级而不是整体失败:快照有时效压力,能留的域先留下。缺的域在快照里
            # 明示,等数据源恢复后重新采集会落新版本
            logger.warning(
                "复盘指标采集:数据源不可用,该域缺席 cycle_id=%s domain=%s",
                cycle_id,
                domain,
                # 合成快照故意不给某个域是预期内的,不需要堆栈
                exc_info=not isinstance(exc, SourceUnavailableError),
            )
            domains[domain] = {"status": STATUS_UNAVAILABLE, "reason": _unavailable_reason(exc)}
            continue
        domains[domain] = {"status": STATUS_OK}
        for item in produced:
            _require_listed(item.id, domain)
            if isinstance(item, Absent):
                absent.append(item)
            else:
                values.extend(suppress(item))
    for spec in CATALOG:
        # 目录里整个域都算不出来的(人机一致),域状态单独标出
        if spec.pending_reason and spec.domain not in domains:
            domains[spec.domain] = {"status": STATUS_PENDING, "reason": "该域指标暂缺,见目录"}

    logger.info(
        "复盘指标采集完成 cycle_id=%s schema=%s metrics=%d unavailable_domains=%s",
        cycle_id,
        METRICS_SCHEMA,
        len(values),
        sorted(d for d, s in domains.items() if s["status"] == STATUS_UNAVAILABLE),
    )
    return {
        "schema": METRICS_SCHEMA,
        "cycle_id": cycle_id,
        "window": (
            {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "timezone": str(CYCLE_TIMEZONE),
            }
            if window
            else None
        ),
        "small_cell_min": SMALL_CELL_MIN,
        "params": {"kb_min_score": kb_min_score},
        "domains": domains,
        "catalog": catalog_payload(),
        "metrics": [asdict(v) for v in sorted(values, key=lambda v: v.id)],
        "absent": [asdict(a) for a in sorted(absent, key=lambda a: a.id)],
    }


def _require_listed(metric_id: str, domain: str) -> None:
    """口径函数产出的指标必须在目录里、属于这个域、且不是暂缺项。"""
    spec = _SPECS.get(metric_id)
    if spec is None or spec.domain != domain or spec.pending_reason:
        raise RuntimeError(f"指标 {metric_id} 不在目录的 {domain} 域可用指标里")
