"""绝对卡确定性短路:进模型前的态度不端硬判,纯函数零 IO。

判定一个维度值为「绝对卡」:全空 / 单字 / 单字符重复(111/。。。) /
纯数字标点 / placeholder 同文(请输入…/字段名本身/无)。任一打分维命中
→ 整份硬 0(attitude=bad_faith),不调模型(确定性规则优先)。

误判兜底:硬 0 只是**重点复核**信号(入 0 分队列,不自动拒),人工评审
可改判——规则宁可略严,由人工评审队列兜底。

模型判完清单后,分数也在这里派生(checklist_score / effort_ceiling):
分数只用于在候选池里排序分级,不直接给人看。
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
# 「我没有这一项」的常见写法:在**可选栏**里与留空同义(获奖经历没奖、实习经历
# 没实习)。不收「同上」「略」——那是把该写的事略掉,不是「没有」。
_NONE_EQUIVALENT = frozenset(
    {"无", "无。", "暂无", "暂无。", "没有", "没有。", "不涉及", "/", "-", "—"}
)


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
    可选栏写「无」与留空是同一件事,同样豁免:表单上这两种表达同义,豁免只做
    一半的话,诚实写「无」的人反而比留空的人整份判零(实测 title=获奖经历、
    required=False 时 `"无"` 就是这个下场)。
    其余规则(抄 placeholder、纯数字、单字)照旧适用:可选栏若填了敷衍内容,
    一样该卡。
    """
    v = (value or "").strip()
    ph = (placeholder or "").strip()
    if not required and (not v or v in _NONE_EQUIVALENT):
        return False
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


def checklist_score(met: dict[str, bool], items: tuple[tuple[str, int], ...]) -> float:
    """清单判定 → 0-10 分:达成项的权重之和(清单权重和为 10)。

    模型只做逐项判定(达成/未达成 + 原文依据),分数在这里算:同一份简历重跑,
    只要判定一致分数就一致。met 的键是**判定项名**,不认位置——模型偶尔打乱
    输出顺序,按位置取值会把权重算到错的项上。清单外的键(模型改了名)不计入。
    """
    total_weight = sum(weight for _, weight in items)
    if not total_weight:
        return 0.0
    got = sum(weight for name, weight in items if met.get(name, False))
    return round(10 * got / total_weight, 1)


#: 简历实质篇幅 → 认真程度上限(0-10)。认真程度的判定项是模型判的:一句话的
#: 简历也可能被判出「表达成文」「具体不套话」而拿到中等分。篇幅是确定性的,
#: 用它封顶,保证材料不到两三行的简历够不上「认真」的档位。
#:
#: 边界按本地样本校准(实质字符数):一段话级别 94-104、两三行 143-269、
#: 完整 429-662。只在**极短**时封顶,不是「越长越认真」:长而空的简历照样
#: 判不出「内容充实」。篇幅是必要条件,不是充分条件。
_EFFORT_CEILINGS: tuple[tuple[int, float], ...] = (
    (130, 3.0),  # 一段话
    (280, 6.0),  # 两三行
)


def effort_ceiling(fields: list[FieldText]) -> float | None:
    """简历篇幅 → 认真程度分上限;None = 不封顶。纯函数,零 IO。

    只量**实质字符数**(原始长度,不做占位剔除):占位与敷衍内容由绝对卡在
    进模型前就拦掉了,这里再剔一遍只会把「短但具体」的简历误伤。
    """
    total = sum(len((f.value or "").strip()) for f in fields)
    for limit, ceiling in _EFFORT_CEILINGS:
        if total < limit:
            return ceiling
    return None
