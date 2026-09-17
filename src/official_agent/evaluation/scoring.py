"""绝对卡确定性短路:进模型前的态度不端硬判,纯函数零 IO。

判定一个维度值为「绝对卡」:全空 / 单字 / 单字符重复(111/。。。) /
纯数字标点 / placeholder 同文(请输入…/字段名本身/无)。任一打分维命中
→ 整份硬 0(attitude=bad_faith),不调模型(确定性规则优先)。

误判兜底:硬 0 只是**初筛不过**信号(入 0 分队列,不自动拒),人工评审
可改判——规则宁可略严,由人工评审队列兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 纯数字/标点(允许分隔符,但必须出现过数字):整栏 111、2024.09 等
# 脱敏产物 138****5678 亦视为纯数字敷衍——掩码前的纯数字
# 敷衍回答不应因掩码引入的 * 而逃过确定性硬 0。
_PURE_DIGIT = re.compile(r"^[\d\s.,，。、\-—_*]+$")
# 常见 placeholder 前缀(表单引导文案)
_PLACEHOLDER_PREFIX = ("请输入", "请填写", "请描述", "请介绍", "在此输入")
# 主观题下的敷衍词(单独回答即绝对卡)
_PLACEHOLDER_EXACT = frozenset({"无", "无。", "暂无", "没有", "同上", "略", ".", "。", "、"})


@dataclass(frozen=True)
class FieldText:
    """一个待评维度。value 是简历原文(已脱敏,PII 不进打分面)。

    placeholder 来自周期字段配置;有精确 placeholder 时
    「与 placeholder 同文」按全等判,比前缀启发式更准。

    required 来自周期字段配置(缺省 True = 保守):**留空是否算问题取决于
    这个字段该不该填**。必填栏留空是候选人没写;可选栏留空只是他没有这一项
    (如「获奖经历」没写奖项),不该据此把整份判成敷衍。
    """

    field_key: str
    title: str
    value: str
    placeholder: str = ""
    required: bool = True


def is_hard_zero_value(
    value: str, *, title: str = "", placeholder: str = "", required: bool = True
) -> bool:
    """单值绝对卡判定。规则序:空 → 单字 → 单字符重复 → 纯数字 → placeholder。

    `required=False` 时,**留空不算绝对卡**:可选栏没填是正常的(没有获奖经历
    就空着),拿它判「敷衍」会把一份认真的简历整份判零 —— 实测过这个后果。
    其余规则(抄 placeholder、纯数字、单字)照旧适用:可选栏若填了敷衍内容,
    一样该卡。
    """
    v = (value or "").strip()
    ph = (placeholder or "").strip()
    return bool(
        (not v and required)
        or (bool(v) and len(v) <= 1)
        or len(set(v)) == 1  # 111、。。。、aaa
        or bool(v and _PURE_DIGIT.fullmatch(v))
        or (bool(v) and v in _PLACEHOLDER_EXACT)
        or bool(ph and v == ph)  # 与配置的 placeholder 全等(最准)
        # 前缀启发式仅在没有配置 placeholder 时兜底(先抄题再作答
        # 会被误卡,有配置时全等判已覆盖)
        or bool(v and not ph and v.startswith(_PLACEHOLDER_PREFIX))
        or bool(title and v == title.strip())  # 抄字段名本身
    )


def detect_hard_zero(fields: list[FieldText]) -> dict[str, str]:
    """扫描全部打分维,返回 {field_key: 命中原因};空 dict = 无绝对卡。"""
    reasons: dict[str, str] = {}
    for f in fields:
        if is_hard_zero_value(
            f.value, title=f.title, placeholder=f.placeholder, required=f.required
        ):
            reasons[f.field_key] = _reason_of(f.value, f.title, placeholder=f.placeholder)
    return reasons


def _reason_of(value: str, title: str, placeholder: str = "") -> str:
    v = (value or "").strip()
    if not v:
        return "空白未填"
    if len(v) <= 1:
        return f"仅单字:{v!r}"
    if len(set(v)) == 1:
        return f"单字符重复:{v[:8]!r}"
    if _PURE_DIGIT.fullmatch(v):
        return f"纯数字:{v[:8]!r}"
    if v in _PLACEHOLDER_EXACT:
        return f"敷衍词:{v!r}"
    if placeholder and v == placeholder.strip():
        return "placeholder 文案未改"
    if v.startswith(_PLACEHOLDER_PREFIX):
        return "placeholder 文案未改"
    if title and v == title.strip():
        return "与字段名同文"
    return "命中绝对卡规则"


#: 达成项数 → 分数段的边界。自高而低,命中第一个不高于达成数的下限即为该段。
#: 分段而非线性(12 项 × 固定分):**顶部要挤、底部要宽**。招新真正想捞的是
#: 「广度都够」的人,所以 11 项以上才进 90 档;而 5 项以下是明显没写东西的
#: 区间,粗分即可。
_BAND_STEPS: tuple[tuple[int, int, int], ...] = (
    # (最低达成数, 分数下限, 分数上限)
    (11, 90, 100),
    (9, 75, 89),
    (7, 60, 74),
    (5, 45, 59),
    (3, 30, 44),
    (1, 15, 29),
    (0, 0, 14),
)


def trait_score(met: dict[str, bool]) -> float:
    """特质达成情况 → 0-100 分(派生,不由模型给分)。

    模型只做逐项判定(达成/未达成 + 依据),分数在这里算:同一份简历重跑,
    只要判定一致分数就一致,不会因为模型这次「心情」不同而漂移。

    met 的键是**特质名**(不是位置):模型偶尔会打乱输出顺序,按位置加权
    会把权重加到错的项上 —— 于是同一个判定集换个顺序就换了分数。
    段内按达成比例微调,同段内也有区分度;前段特质达成得多的落在段内偏高。
    """
    if not met:
        return 0.0
    # 只统计清单内的项:多余键(模型改名)不计入,避免虚增达成数
    from official_agent.evaluation.schema import TRAITS

    flags = [bool(met.get(name, False)) for name in TRAITS]
    n = len(flags)
    # 权重按**清单顺序**递减(第一项最重):靠前的特质是招新更看重的
    weights = [n - i for i in range(n)]
    met_count = sum(1 for m in flags if m)
    weighted = sum(w for w, m in zip(weights, flags, strict=True) if m)
    weighted_max = sum(weights)

    for min_met, low, high in _BAND_STEPS:
        if met_count >= min_met:
            ratio = weighted / weighted_max if weighted_max else 0.0
            return round(low + (high - low) * ratio, 1)
    return 0.0


def weighted_total(scores: dict[str, int], weights: dict[str, float]) -> float:
    """加权总分(派生,非模型输出):Σ 分×权 / Σ 权;权重缺省 1.0。

    保留给仍按维度给分的调用方(周期级维度配置);简历初筛已改用
    `trait_score` 的清单分段。
    """
    num = 0.0
    den = 0.0
    for key, score in scores.items():
        w = float(weights.get(key, 1.0))
        num += score * w
        den += w
    return round(num / den, 1) if den else 0.0
