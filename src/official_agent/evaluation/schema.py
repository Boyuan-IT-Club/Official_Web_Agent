"""评分输出契约:仓库首个 strict Pydantic 结构化输出。

strict 语义:extra="forbid" + 字段约束。输出轨为提示词 JSON + 本 schema
校验(实测:当前代理模型全为思考模式,json_schema response_format
与强制 tool_choice 都被 400 拒)——schema 即提示词的一部分,字段名/枚举值
改一个字模型行为就变,所以本文件是 prompt 级资产。

**一份简历一个总分**,不再逐栏打分:逐栏平均会把「哪一栏长」当成「人好不好」,
一句话的候选人只要那一句写得顺就能追平有开源项目的人。改为按**特质清单**
逐项判定达成与否,达成项数决定分数段——总分对应的是「这个人具备几项我们
看重的品质」,而不是「他的字写得好不好」。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 招新看重的特质清单。**顺序即权重递减**,也是 prompt 里逐项判定的顺序。
#: 收项原则:能凭简历原文**判定达成与否**的才收(「真诚」「有热情」这类无法
#: 从文字证伪的,归到最后一项「内容详实」里一并体现)。
TRAITS: tuple[str, ...] = (
    "经验丰富",  # 有可讲的真实项目/实习/竞赛经历
    "技术能力",  # 会写代码:coding / vibe-coding / 工程工具链
    "自学能力",  # 主动学过课外的技术,有路径有产出
    "开源精神",  # 公开作品、提交记录、文档沉淀
    "一技之长",  # 技术之外的手艺:运营/剪辑/设计/洽谈/统筹
    "对技术的热情",  # 主动钻研的痕迹(读源码、打 CTF、造轮子)
    "对社团的热情",  # 想清楚为什么来这个社团、能带来什么
    "责任心",  # 有始有终、带过人、担过责
    "学业表现",  # 绩点/奖学金/学业类奖项
    "内容详实",  # 每栏都写、有细节有数字,不是一两句敷衍
    "真诚",  # 不套话不夸大,写自己的真实水平与动机
    "表达与结构",  # 条理清楚,读得下去
)


class TraitVerdict(BaseModel):
    """一项特质的判定结果。

    两个字段分工明确,**不要混**:
    - `reason`:自然语言依据,写给人看(判 false 时写**缺什么**,不许空着)。
      它是总结,不是引文,机器不逐字校验——总结本来就不该等于原文。
    - `quote`:判定所依据的**原文逐字片段**,判 true 时必填。机器只校验这一项:
      编造的原文在简历里找不到。分开之后,「防编造」与「用自然语言解释」
      各走各的字段,不会因为要求人话说得像原文而互相打架。
    """

    model_config = ConfigDict(extra="forbid")

    trait: str = Field(min_length=1, description=f"特质名,取值:{' / '.join(TRAITS)}")
    met: bool
    quote: str = Field(
        default="",
        max_length=200,
        description="判定依据的原文逐字片段(判 true 必填);不要改写、不要拼接多处",
    )
    reason: str = Field(min_length=1, description="自然语言依据;判 false 时写缺什么")


class AttitudeVerdict(BaseModel):
    """态度结论:端正(sincere)/敷衍(perfunctory,总分压低)/不端(bad_faith,整份 0)。"""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["sincere", "perfunctory", "bad_faith"]
    reason: str = Field(min_length=1)


class ScorecardOutput(BaseModel):
    """模型结构化输出整体;与确定性规则合并后落 evaluation_scorecard 卡。

    分数由**达成项数**派生(代码算,不让模型直接给分):模型只负责逐项判定
    达成与否,把「给几分」这种容易漂移的判断换成可核对的清单。
    """

    model_config = ConfigDict(extra="forbid")

    traits: list[TraitVerdict] = Field(min_length=1)
    summary: str = Field(min_length=1, description="2-4 句整体评价,给面试官看的理由")
    attitude: AttitudeVerdict


class QuestionEvidence(BaseModel):
    """证据锚:仓内可点路径(deep_dive 必填);无仓引导题留空+note 说明。"""

    model_config = ConfigDict(extra="ignore")

    path: str = ""
    note: str = ""


class AnswerReference(BaseModel):
    """参考答案三锚:面试官据此判断答得算好/达标/弱。"""

    model_config = ConfigDict(extra="ignore")

    strong: str = Field(min_length=1)
    acceptable: str = Field(min_length=1)
    weak: str = Field(min_length=1)


class InterviewQuestion(BaseModel):
    """单道预置面试题(envelope 核心;qbank 落库的最小单元)。"""

    model_config = ConfigDict(extra="forbid")

    anchor: Literal["architecture", "claims_vs_reality", "edge_case", "tradeoff", "guided"]
    question: str = Field(min_length=1)
    sub_prompts: list[str] = Field(default_factory=list, max_length=5)
    answer_reference: AnswerReference
    evidence: QuestionEvidence
    time_minutes: int = Field(default=3, ge=2, le=5)


class QuestionSet(BaseModel):
    """一次调查产出的题集;questions 空 = skip/零信号(合法)。

    mode/prompt_version 是信封字段(非模型输出,生成后注入)——类型化进
    schema,让下游 qbank 拿到的形状可通过自身校验。
    """

    model_config = ConfigDict(extra="forbid")

    repo_summary: str = ""
    questions: list[InterviewQuestion] = Field(default_factory=list, max_length=6)
    mode: Literal["repo_deep_dive", "cv_dive", "guided", "skipped"] | None = None
    prompt_version: str = ""


# ── 题组 schema v2(直接替换,不兼容旧 questions 形状) ──

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

#: 题组容量的**指导值**(prompt 侧写给模型的配额;2026-09 起不再是拒绝
#: 条件——生产实测模型偶发超限一两条,硬拦等于整组归零,超出照存)。
#: MAX_CHAINS 同时还是链数下界(≥2)的校验文案参照。改值只影响 prompt
#: 与文案,不会改变保存行为。
MAX_CHAINS = 4
MAX_RESERVES = 6

#: 每链层数的指导值。层数的**深度策略**(标准档 3-5、大一档 1-2)由语义
#: 校验按档位把关(LAYER_BOUNDS),schema 不再设形状上界。
MAX_CHAIN_LAYERS = 5


class ChainLayer(BaseModel):
    """追问链的一层:问题 + expected_signal(答到什么算过;层间依赖)。

    answer_reference 可选:技术栈题要逐层给三档参考答案(面试官本人可能
    不熟悉该技术,没有参考答案就无法判定答得好不好);仓深挖的链层沿用
    既有形状(只有 expected_signal),故为可选而非必填。
    """

    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1)
    expected_signal: str = Field(min_length=1)
    answer_reference: AnswerReference | None = None


class QuestionChain(BaseModel):
    """追问链:层层依赖的连环问(下一问以上一问的回答为前提)。

    theme 对两种路径都必填,但含义不同:
    - 仓深挖:须指明源自哪条 dossier 证据(仓内路径/组件);
    - 简历深挖:填**该链对应的技术名词**,title 即技术名。
      这样防编造校验(链源须出现在材料里)天然生效——编造的技术名过不了。
    """

    model_config = ConfigDict(extra="ignore")

    category: CATEGORY
    theme: str = Field(min_length=1, description="链主题(仓路径:证据出处;简历路径:技术名词)")
    layers: list[ChainLayer] = Field(min_length=1)


class EntryQuestion(BaseModel):
    """入口题:题组 opener(通常 C1/C3,热身+定基调)。"""

    model_config = ConfigDict(extra="ignore")

    category: CATEGORY
    question: str = Field(min_length=1)
    answer_reference: AnswerReference
    evidence: QuestionEvidence
    time_minutes: int = Field(default=3, ge=2, le=5)


class ReserveQuestion(BaseModel):
    """备选题:面试官按候选人回答灵活取用,不强制走完。

    简历深挖里它承载**技术栈的独立广度题**——与项目深挖链分开,面试官可按
    现场挑着问。
    """

    model_config = ConfigDict(extra="ignore")

    category: CATEGORY
    question: str = Field(min_length=1)
    answer_reference: AnswerReference
    evidence: QuestionEvidence
    time_minutes: int = Field(default=3, ge=2, le=5)


class QuestionGroupV2(BaseModel):
    """一个题组:入口 1 + 追问链 + 备选;guided 模式 chains/reserves 可空。

    数量上限(chains/layers/reserves 的 max)已从 schema **撤下**:生产
    实测模型偶发超限一两条(5 链/备选混入链层字段),硬拦等于整组归零,
    代价与收益完全不成比例——超出照存,题多面试官自己挑。MAX_* 常量
    降级为 prompt 指导值与语义校验的下界参照,不再是拒绝条件。
    extra 同理放宽为 ignore:形状小 slip(备选混进 expected_signal)剥掉
    多余字段即可,题目本身无辜。内容质量(防编造/对抗前提/三档答案)
    仍由校验机器全量把关,与此无关。
    """

    model_config = ConfigDict(extra="ignore")

    entry: EntryQuestion | None = None
    chains: list[QuestionChain] = Field(default_factory=list)
    reserves: list[ReserveQuestion] = Field(default_factory=list)

    @property
    def total_questions(self) -> int:
        return (
            (1 if self.entry else 0) + sum(len(c.layers) for c in self.chains) + len(self.reserves)
        )


class ExploreMeta(BaseModel):
    """探索段元信息(可观测/可展示;用量管道接会话用量面板)。

    cache 命中/未命中取 DeepSeek prompt_cache 语义(extract_usage)。"""

    model_config = ConfigDict(extra="forbid")

    turns: int = 0
    dossier_chars: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


class UsageMeta(BaseModel):
    """单次/聚合 LLM 用量(None=未采集,fail-open)。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


class QbankV2(BaseModel):
    """调查出题信封 v2(evaluation_qbank/v2,直接替换不兼容)。

    attribution/degraded 是信封一等概念(ADR-0008:unverified 绝不出仓题;
    预算触顶 degraded 出题)。mode/guide 沿用旧语义:guided=仓库材料
    缺失的通用引导组(此时 entry 可以是引导题,chains 空)。
    """

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["evaluation_qbank/v2"] = "evaluation_qbank/v2"
    repo_summary: str = ""
    group: QuestionGroupV2
    mode: Literal["repo_deep_dive", "cv_dive", "guided", "skipped"] | None = None
    attribution: ATTRIBUTION_LEVEL = "none"
    degraded: bool = False
    degrade_reason: str = ""
    explore_meta: ExploreMeta = Field(default_factory=ExploreMeta)
    generation_usage: UsageMeta | None = None  # 出题段单次调用用量
    prompt_version: str = ""


# ── LLM-as-judge 出题质量报告(首版只报告不阻塞) ──

JUDGE_DIMENSION = Literal["relevance", "specificity", "fairness", "differentiation"]


class JudgeDimensionScore(BaseModel):
    """judge 单维评分:1-5 分 + 引用具体题目的理由。"""

    model_config = ConfigDict(extra="forbid")

    dimension: JUDGE_DIMENSION
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1)


class JudgeReport(BaseModel):
    """judge 报告整体;阈值待校准后才转门禁。"""

    model_config = ConfigDict(extra="forbid")

    dimensions: list[JudgeDimensionScore] = Field(min_length=4, max_length=4)
    overall: str = ""

    @model_validator(mode="after")
    def _four_distinct_dimensions(self) -> "JudgeReport":
        dims = [d.dimension for d in self.dimensions]
        if len(set(dims)) != 4:
            raise ValueError(f"四维必须互异且齐全:{dims}")
        return self
