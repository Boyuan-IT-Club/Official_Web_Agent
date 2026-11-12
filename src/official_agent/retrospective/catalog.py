"""指标目录:周期指标快照里每个指标的稳定 id、名字与口径说明。纯数据,零 IO。

口径怎么算在 retrospective/metrics.py 的各域函数里;这里只管「这个数叫什么、
指的是什么」。任何一个指标的口径、id 或抑制规则变了都要升 METRICS_SCHEMA——
不同版本的快照不能对比(见 retrospective/compare.py)。

目录随每份快照一起存:多年后读快照的人不必翻当年的代码找口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from official_agent.retrospective.guard import UNIT_COUNT, UNIT_MS, UNIT_RATIO, UNIT_TOKENS

#: 指标定义的版本。任何一个指标的口径、id 或抑制规则变了都要升;
#: 升了之后新旧快照不再对比。
METRICS_SCHEMA = "cycle_metrics/v2"

DOMAIN_FUNNEL = "funnel"
DOMAIN_CONVERSATION = "conversation"
DOMAIN_KB = "kb"
DOMAIN_SCREENING = "screening"
DOMAIN_AGREEMENT = "agreement"
DOMAIN_QBANK = "qbank"
DOMAIN_COST = "cost"

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"  # 这次采集时数据源不可用
STATUS_PENDING = "pending"  # 这个域的指标目前都算不出来(缺数据或口径未定)


@dataclass(frozen=True)
class MetricSpec:
    """指标目录的一项。

    pending_reason 非空表示这个指标目前算不出来,值是原因;这样的指标只出现在
    目录里,不出现在快照的 metrics 里。
    """

    id: str
    domain: str
    name: str
    definition: str
    unit: str
    pending_reason: str = ""


CATALOG: tuple[MetricSpec, ...] = (
    # ── 漏斗:后端的投递与面试结果,与对话工具 get_recruit_statistics 同一份统计 ──
    MetricSpec(
        "funnel.resumes_total",
        DOMAIN_FUNNEL,
        "投递总数",
        "该周期的简历总数,含未提交的草稿(后端简历检索的总条数)。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "funnel.results_total",
        DOMAIN_FUNNEL,
        "面试结果条数",
        "该周期面试结果列表的条数,是「各决定计数」的合计。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "funnel.decisions",
        DOMAIN_FUNNEL,
        "各决定计数",
        "面试结果按最终决定分格:passed 通过、rejected 不通过、pending 待定、"
        "toTransfer 待调剂;认不出的决定码计入待定。通过与不通过两格固定出,"
        "待定与待调剂没有人时不出格子。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "funnel.assigned_by_dept",
        DOMAIN_FUNNEL,
        "各分配部门计数",
        "面试结果按分配部门分格,键是后端的部门 id(结果列表不带部门名);"
        "还没分配部门的归 unassigned 一格,没有这样的人时不出这一格。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "funnel.evaluated_candidates",
        DOMAIN_FUNNEL,
        "已评价人数",
        "后端面试评价汇总里的候选人数;该周期没开评价表时没有这个数。",
        UNIT_COUNT,
    ),
    # ── 对话:agent_conversation_log,按周期时间窗切 ──
    MetricSpec(
        "conversation.turns",
        DOMAIN_CONVERSATION,
        "对话轮数",
        "周期时间窗内的对话日志行数,一轮一行;不含评估任务写的用量行。"
        "已被用户删除或过期清理的会话不在其中(对话域各指标都如此)。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "conversation.sessions",
        DOMAIN_CONVERSATION,
        "会话数",
        "这些对话轮次分属多少个会话。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "conversation.turns_by_channel",
        DOMAIN_CONVERSATION,
        "渠道分布",
        "对话轮数按渠道(web、cli 等)分格。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "conversation.error_rate",
        DOMAIN_CONVERSATION,
        "错误率",
        "以错误收场(记了错误码)的轮数占对话轮数的比例。",
        UNIT_RATIO,
    ),
    MetricSpec(
        "conversation.duration_p50_ms",
        DOMAIN_CONVERSATION,
        "单轮耗时中位数",
        "有耗时记录的轮次里,单轮耗时的第 50 百分位(毫秒)。",
        UNIT_MS,
    ),
    MetricSpec(
        "conversation.duration_p90_ms",
        DOMAIN_CONVERSATION,
        "单轮耗时 P90",
        "同上,第 90 百分位。",
        UNIT_MS,
    ),
    MetricSpec(
        "conversation.duration_p99_ms",
        DOMAIN_CONVERSATION,
        "单轮耗时 P99",
        "同上,第 99 百分位。",
        UNIT_MS,
    ),
    MetricSpec(
        "conversation.tool_calls",
        DOMAIN_CONVERSATION,
        "工具使用分布",
        "各工具被调用的次数;一轮里同一个工具调用几次就算几次。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "conversation.input_tokens",
        DOMAIN_CONVERSATION,
        "输入 token",
        "对话轮次的输入 token 合计;样本量是有 token 记录的轮数。",
        UNIT_TOKENS,
    ),
    MetricSpec(
        "conversation.output_tokens",
        DOMAIN_CONVERSATION,
        "输出 token",
        "对话轮次的输出 token 合计;样本量是有 token 记录的轮数。",
        UNIT_TOKENS,
    ),
    MetricSpec(
        "conversation.cache_hit_rate",
        DOMAIN_CONVERSATION,
        "缓存命中率",
        "缓存命中 token ÷(命中 + 未命中 token),只算两项都有记录的轮次;"
        "样本量是这些轮次的数目。",
        UNIT_RATIO,
    ),
    # ── 知识库:kb_query_log,按周期时间窗切 ──
    MetricSpec(
        "kb.searches",
        DOMAIN_KB,
        "检索次数",
        "周期时间窗内对话里查知识库的次数;随会话删除的检索记录不在其中。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "kb.not_found_rate",
        DOMAIN_KB,
        "未命中率",
        "结局为没查到(not_found)的检索占全部检索的比例。",
        UNIT_RATIO,
    ),
    MetricSpec(
        "kb.unavailable_rate",
        DOMAIN_KB,
        "检索不可用率",
        "结局为不可用(嵌入端点或库故障)的检索占全部检索的比例;"
        "读未命中率时要一并看它,这类检索既不算命中也不算未命中。",
        UNIT_RATIO,
    ),
    MetricSpec(
        "kb.low_score_hit_rate",
        DOMAIN_KB,
        "低分命中率",
        "命中的检索里,最高向量分低于向量门槛(采集时的 kb_min_score,见快照的 "
        "params)的比例——这类命中只靠关键词门槛过关。门槛中途调过的话,"
        "这里一律按采集时的值算。",
        UNIT_RATIO,
    ),
    MetricSpec(
        "kb.top_topics",
        DOMAIN_KB,
        "没答好的高频主题",
        "知识库没答上的提问聚成的主题及其检索次数,只列甄别为「该由知识库回答」的,"
        "按次数取前十;每项带主题名(模型对多人提问的概括,不是原话)与缺口类型"
        "(missing 库里多半没有 / wording 库里多半有、问法没对上)。主题取自与周期"
        "时间窗有重叠的最近一次成功的知识库离线分析,次数是那次分析的时间窗内的"
        "(见快照的 params),不是整个周期的。检索次数或会话数不足 5 的主题不列。",
        UNIT_COUNT,
    ),
    # ── 初筛:evaluation_job 与 evaluation_scorecard,按 cycle_id 取 ──
    MetricSpec(
        "screening.jobs",
        DOMAIN_SCREENING,
        "初筛任务数",
        "该周期的初筛任务行数;一份简历重新触发一次就多一行。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "screening.job_failure_rate",
        DOMAIN_SCREENING,
        "任务失败率",
        "当前状态为失败的任务占全部任务的比例;失败后重试成功的不算,"
        "还在排队或执行中的算在分母里。",
        UNIT_RATIO,
    ),
    MetricSpec(
        "screening.jobs_by_attempts",
        DOMAIN_SCREENING,
        "重试分布",
        "任务按实际执行次数分格;1 是一次跑完,2 及以上是重试过。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "screening.scored_resumes",
        DOMAIN_SCREENING,
        "已出卡简历数",
        "该周期至少有一版评分卡的简历数。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "screening.hard_zero_rate",
        DOMAIN_SCREENING,
        "硬零分率",
        "每份简历取最新一版评分卡,其中标了需重点复核(hard_zero)的比例。",
        UNIT_RATIO,
    ),
    MetricSpec(
        "screening.rescore_count",
        DOMAIN_SCREENING,
        "重评次数",
        "评分卡版本总数减去已出卡简历数:一份简历第一版之后每多一版算一次重评。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "screening.decisions",
        DOMAIN_SCREENING,
        "采纳与驳回",
        "已出卡的简历按人工决定分格:adopted 采纳、rejected 驳回、undecided 还没"
        "决定。取每份简历历史上最后一次人工决定,之后复评写出的新卡不抹掉它。"
        "采纳与驳回两格固定出,都决定完了就没有 undecided 这一格。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "screening.adopted_by_dept",
        DOMAIN_SCREENING,
        "采纳(按部门)",
        "被采纳的简历按第一志愿部门分格,每个部门的格子固定出;"
        "卡上没有志愿的归「未填志愿」,没有这样的简历时不出这一格。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "screening.rejected_by_dept",
        DOMAIN_SCREENING,
        "驳回(按部门)",
        "被驳回的简历按第一志愿部门分格,每个部门的格子固定出;"
        "卡上没有志愿的归「未填志愿」,没有这样的简历时不出这一格。",
        UNIT_COUNT,
    ),
    # ── 人机一致:evaluation_scorecard 上的置信度对照人工决定 ──
    MetricSpec(
        "agreement.rank_agreement_by_dept",
        DOMAIN_AGREEMENT,
        "人机排序一致率",
        "各部门候选池内,模型匹配分的排序与人工信号排序的一致程度。",
        UNIT_RATIO,
        pending_reason="人工信号用哪一个还没定:采纳时填的分、最终录取决定、面试评价"
        "三者含义不同(docs/proposals/cycle-retrospective.md 开放问题 2)。",
    ),
    MetricSpec(
        "agreement.low_confidence_overturn_rate",
        DOMAIN_AGREEMENT,
        "低置信卡片改判率",
        "置信度为低的评分卡里,被人工驳回的比例。每份简历取历史上最后一次人工决定"
        "所在的那张卡(与「采纳/驳回」同口径);分母是其中置信度为低的,分子是被驳回的。"
        "采纳时改了分不算改判。单评委模式出的卡没有置信度,不计入;整个周期的已决卡"
        "都没有置信度时这一项缺席。读的时候对照「采纳/驳回/未决」里的整体驳回情况。",
        UNIT_RATIO,
    ),
    # ── 出题:interview_qbank 与 qbank_pick_log,按 cycle_id 取 ──
    MetricSpec(
        "qbank.generated",
        DOMAIN_QBANK,
        "题库生成数",
        "该周期有预置题库的简历数。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "qbank.degraded_rate",
        DOMAIN_QBANK,
        "降级率",
        "每份简历取最新一版题库,其中带降级标记的比例:任一仓库题组在探索时"
        "预算触顶,或没取到材料、改用通用引导题。",
        UNIT_RATIO,
    ),
    MetricSpec(
        "qbank.picked_resumes",
        DOMAIN_QBANK,
        "有选题记录的简历数",
        "面试官至少勾选过一道预置题的简历数,是「选用率」的统计范围。",
        UNIT_COUNT,
    ),
    MetricSpec(
        "qbank.pick_rate_by_group",
        DOMAIN_QBANK,
        "各题组类别的选用率",
        "只看有选题记录的简历:某类题组里被勾选的题数 ÷ 该类题组可挑的题数,"
        "都按这些简历最新一版题库算。题库在选题之后重新生成过的,旧版本上的"
        "选题记录不计。不按面试官拆。",
        UNIT_RATIO,
    ),
    # ── 成本:只有 token,仓库里没有计价表,不折算金额 ──
    MetricSpec(
        "cost.conversation_tokens",
        DOMAIN_COST,
        "对话域 token 总量",
        "对话轮次的输入与输出 token 之和;样本量是有 token 记录的轮数。",
        UNIT_TOKENS,
    ),
    MetricSpec(
        "cost.evaluation_tokens",
        DOMAIN_COST,
        "评估域 token 总量(题库段)",
        "初筛任务题库段(仓库探索与出题)的输入与输出 token 之和;"
        "不含评分段。样本量是有用量记录的简历数。",
        UNIT_TOKENS,
    ),
    MetricSpec(
        "cost.evaluation_tokens_per_resume",
        DOMAIN_COST,
        "单份简历平均 token(题库段)",
        "评估域 token 总量(题库段)÷ 有用量记录的简历数,四舍五入到整数。",
        UNIT_TOKENS,
    ),
    MetricSpec(
        "cost.scoring_tokens",
        DOMAIN_COST,
        "评估域 token 总量(评分段)",
        "初筛任务评分段的输入与输出 token 之和,一张卡由几次模型调用产出就合计几次"
        "(评委、批判者、答辩、主席);同一份简历重评几次就计几次。样本量是有用量"
        "记录的简历数。评分段用量落库之前跑完的任务没有记录,不在其中。",
        UNIT_TOKENS,
    ),
)


def catalog_payload() -> list[dict[str, Any]]:
    """目录的可序列化形态;随快照一起存,多年后读快照的人不必翻代码找口径。"""
    return [
        {
            "id": spec.id,
            "domain": spec.domain,
            "name": spec.name,
            "definition": spec.definition,
            "unit": spec.unit,
            "status": STATUS_PENDING if spec.pending_reason else STATUS_OK,
            "reason": spec.pending_reason,
        }
        for spec in CATALOG
    ]
