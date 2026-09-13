"""绝对卡确定性短路(B1,#123):进模型前的态度不端硬判,纯函数零 IO。

判定一个维度值为「绝对卡」:全空 / 单字 / 单字符重复(111/。。。) /
纯数字标点 / placeholder 同文(请输入…/字段名本身/无)。任一打分维命中
→ 整份硬 0(attitude=bad_faith),不调模型(#123:确定性规则优先)。

误判兜底:硬 0 只是**初筛不过**信号(入 0 分队列,不自动拒),人工评审
可改判——规则宁可略严,由 B6 评审队列兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 纯数字/标点(允许分隔符,但必须出现过数字):整栏 111、2024.09 等
# #176 评审:脱敏产物 138****5678 亦视为纯数字敷衍——掩码前的纯数字
# 敷衍回答不应因掩码引入的 * 而逃过确定性硬 0。
_PURE_DIGIT = re.compile(r"^[\d\s.,，。、\-—_*]+$")
# 常见 placeholder 前缀(表单引导文案)
_PLACEHOLDER_PREFIX = ("请输入", "请填写", "请描述", "请介绍", "在此输入")
# 主观题下的敷衍词( standalone 回答即绝对卡)
_PLACEHOLDER_EXACT = frozenset({"无", "无。", "暂无", "没有", "同上", "略", ".", "。", "、"})


@dataclass(frozen=True)
class FieldText:
    """一个待评维度。value 是简历原文(已脱敏,PII 不进打分面,#123)。

    placeholder 来自周期字段配置(B2 接线);有精确 placeholder 时
    「与 placeholder 同文」按全等判,比前缀启发式更准。
    """

    field_key: str
    title: str
    value: str
    placeholder: str = ""


def is_hard_zero_value(value: str, *, title: str = "", placeholder: str = "") -> bool:
    """单值绝对卡判定。规则序:空 → 单字 → 单字符重复 → 纯数字 → placeholder。"""
    v = (value or "").strip()
    ph = (placeholder or "").strip()
    return bool(
        not v
        or len(v) <= 1
        or len(set(v)) == 1  # 111、。。。、aaa
        or bool(_PURE_DIGIT.fullmatch(v))
        or v in _PLACEHOLDER_EXACT
        or bool(ph and v == ph)  # 与配置的 placeholder 全等(最准)
        # 前缀启发式仅在没有配置 placeholder 时兜底(评审 P2:先抄题再作答
        # 会被误卡,有配置时全等判已覆盖)
        or (not ph and v.startswith(_PLACEHOLDER_PREFIX))
        or bool(title and v == title.strip())  # 抄字段名本身
    )


def detect_hard_zero(fields: list[FieldText]) -> dict[str, str]:
    """扫描全部打分维,返回 {field_key: 命中原因};空 dict = 无绝对卡。"""
    reasons: dict[str, str] = {}
    for f in fields:
        if is_hard_zero_value(f.value, title=f.title, placeholder=f.placeholder):
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


def weighted_total(scores: dict[str, int], weights: dict[str, float]) -> float:
    """加权总分(派生,非模型输出):Σ 分×权 / Σ 权;权重缺省 1.0。"""
    num = 0.0
    den = 0.0
    for key, score in scores.items():
        w = float(weights.get(key, 1.0))
        num += score * w
        den += w
    return round(num / den, 1) if den else 0.0
