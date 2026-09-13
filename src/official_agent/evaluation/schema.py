"""评分输出契约(B1,#123):仓库首个 strict Pydantic 结构化输出。

strict 语义:extra="forbid" + 字段约束。输出轨为提示词 JSON + 本 schema
校验(检查点③实测:当前代理模型全为思考模式,json_schema response_format
与强制 tool_choice 都被 400 拒)——schema 即提示词的一部分,字段名/枚举值
改一个字模型行为就变,所以本文件是 prompt 级资产。

维度分 0-100(内部锚),总分是派生值(代码加权),不由模型给。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DimensionScore(BaseModel):
    """单维评分:分 + 依据 + 原文证据句。

    evidence 必须是该维 textarea 的原文片段(句级,#123)——评审复核的锚,
    模型编造证据时评委可直接对照简历打回。
    """

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1, description="简历字段键(周期配置驱动)")
    score: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1, description="为什么给这个分")
    evidence: str = Field(min_length=1, max_length=120, description="该维原文句,逐字引用")


class AttitudeVerdict(BaseModel):
    """态度结论:端正(sincere)/敷衍(perfunctory,各维压低)/不端(bad_faith,整份 0)。"""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["sincere", "perfunctory", "bad_faith"]
    reason: str = Field(min_length=1)


class ScorecardOutput(BaseModel):
    """模型结构化输出整体;与确定性规则合并后落 evaluation_scorecard 卡。"""

    model_config = ConfigDict(extra="forbid")

    dimensions: list[DimensionScore] = Field(min_length=1)
    attitude: AttitudeVerdict


class QuestionEvidence(BaseModel):
    """证据锚:仓内可点路径(deep_dive 必填);无仓引导题留空+note 说明。"""

    model_config = ConfigDict(extra="forbid")

    path: str = ""
    note: str = ""


class AnswerReference(BaseModel):
    """参考答案三锚(#125/#127):面试官据此判断答得算好/达标/弱。"""

    model_config = ConfigDict(extra="forbid")

    strong: str = Field(min_length=1)
    acceptable: str = Field(min_length=1)
    weak: str = Field(min_length=1)


class InterviewQuestion(BaseModel):
    """单道预置面试题(envelope 核心;B5 qbank 落库的最小单元)。"""

    model_config = ConfigDict(extra="forbid")

    anchor: Literal["architecture", "claims_vs_reality", "edge_case", "tradeoff", "guided"]
    question: str = Field(min_length=1)
    sub_prompts: list[str] = Field(default_factory=list, max_length=5)
    answer_reference: AnswerReference
    evidence: QuestionEvidence
    time_minutes: int = Field(default=3, ge=2, le=5)


class QuestionSet(BaseModel):
    """一次调查产出的题集;questions 空 = skip/零信号(合法,#130)。

    mode/prompt_version 是信封字段(非模型输出,生成后注入)——类型化进
    schema,让 B5 qbank 拿到的形状可通过自身校验(B3 评审 P2)。
    """

    model_config = ConfigDict(extra="forbid")

    repo_summary: str = ""
    questions: list[InterviewQuestion] = Field(default_factory=list, max_length=6)
    mode: Literal["repo_deep_dive", "guided", "skipped"] | None = None
    prompt_version: str = ""


# ── 题组 schema v2(#152;D12/D13 直接替换,不兼容旧 questions 形状) ──

CATEGORY = Literal[
    "C1_背景与动机",
    "C2_技术选型与权衡",
    "C3_架构与数据流",
    "C4_实现细节拷打",
    "C5_数字与规模",
    "C6_难点与调试",
    "C7_边界与失败模式",
    "C8_真实性与贡献边界",
    "C9_变更条件",
    "C10_复盘与改进",
]

ATTRIBUTION_LEVEL = Literal["trusted-own", "trusted-contribution", "claimed", "unverified", "none"]


class ChainLayer(BaseModel):
    """追问链的一层:问题 + expected_signal(答到什么算过;层间依赖)。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    expected_signal: str = Field(min_length=1)


class QuestionChain(BaseModel):
    """追问链:层层依赖的连环问(下一问以上一问的回答为前提,D12)。"""

    model_config = ConfigDict(extra="forbid")

    category: CATEGORY
    theme: str = Field(min_length=1, description="链主题,须指明源自哪条 dossier 证据")
    layers: list[ChainLayer] = Field(min_length=3, max_length=5)


class EntryQuestion(BaseModel):
    """入口题:题组 opener(通常 C1/C3,热身+定基调)。"""

    model_config = ConfigDict(extra="forbid")

    category: CATEGORY
    question: str = Field(min_length=1)
    answer_reference: AnswerReference
    evidence: QuestionEvidence
    time_minutes: int = Field(default=3, ge=2, le=5)


class ReserveQuestion(BaseModel):
    """备选题:面试官按候选人回答灵活取用,不强制走完。"""

    model_config = ConfigDict(extra="forbid")

    category: CATEGORY
    question: str = Field(min_length=1)
    answer_reference: AnswerReference
    evidence: QuestionEvidence
    time_minutes: int = Field(default=3, ge=2, le=5)


class QuestionGroupV2(BaseModel):
    """一个仓的题组:入口 1 + 追问链 2-4 + 备选 2-3(D12);guided 模式 chains/reserves 可空。"""

    model_config = ConfigDict(extra="forbid")

    entry: EntryQuestion | None = None
    chains: list[QuestionChain] = Field(default_factory=list, max_length=4)
    reserves: list[ReserveQuestion] = Field(default_factory=list, max_length=3)

    @property
    def total_questions(self) -> int:
        return (
            (1 if self.entry else 0) + sum(len(c.layers) for c in self.chains) + len(self.reserves)
        )


class ExploreMeta(BaseModel):
    """探索段元信息(可观测/可展示;D9 用量管道接 M6,#154)。

    cache 命中/未命中取 DeepSeek prompt_cache 语义(extract_usage)。"""

    model_config = ConfigDict(extra="forbid")

    turns: int = 0
    dossier_chars: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


class UsageMeta(BaseModel):
    """单次/聚合 LLM 用量(D9;None=未采集,fail-open)。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


class QbankV2(BaseModel):
    """调查出题信封 v2(D13:evaluation_qbank/v2,直接替换不兼容)。

    attribution/degraded 是信封一等概念(ADR-0008:unverified 绝不出仓题;
    D7:预算触顶 degraded 出题)。mode/guide 沿用旧语义:guided=仓库材料
    缺失的通用引导组(此时 entry 可以是引导题,chains 空)。
    """

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["evaluation_qbank/v2"] = "evaluation_qbank/v2"
    repo_summary: str = ""
    group: QuestionGroupV2
    mode: Literal["repo_deep_dive", "guided", "skipped"] | None = None
    attribution: ATTRIBUTION_LEVEL = "none"
    degraded: bool = False
    degrade_reason: str = ""
    explore_meta: ExploreMeta = Field(default_factory=ExploreMeta)
    generation_usage: UsageMeta | None = None  # 出题段单次调用用量(#154)
    prompt_version: str = ""


# ── LLM-as-judge 出题质量报告(#155/#62;首版只报告不阻塞) ──

JUDGE_DIMENSION = Literal["relevance", "specificity", "fairness", "differentiation"]


class JudgeDimensionScore(BaseModel):
    """judge 单维评分:1-5 分 + 引用具体题目的理由。"""

    model_config = ConfigDict(extra="forbid")

    dimension: JUDGE_DIMENSION
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1)


class JudgeReport(BaseModel):
    """judge 报告整体;阈值等 AG8(#156)校准后才转门禁。"""

    model_config = ConfigDict(extra="forbid")

    dimensions: list[JudgeDimensionScore] = Field(min_length=4, max_length=4)
    overall: str = ""

    @model_validator(mode="after")
    def _four_distinct_dimensions(self) -> "JudgeReport":
        dims = [d.dimension for d in self.dimensions]
        if len(set(dims)) != 4:
            raise ValueError(f"四维必须互异且齐全:{dims}")
        return self
