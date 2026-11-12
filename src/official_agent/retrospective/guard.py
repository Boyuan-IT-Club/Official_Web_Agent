"""小格子抑制:原始聚合变成快照里的数之前的唯一出口。纯函数,零 IO。

快照长期保留、之后还要进报告,所以能落到个人头上的小计数不许出现在里面
(docs/proposals/cycle-retrospective.md D4)。各域算出的原始聚合(Raw*)只能经
suppress() 变成 MetricValue,快照只收 MetricValue——没有绕过抑制的路径。

规则(阈值 SMALL_CELL_MIN):
- 计数:小于阈值即抑制,0 也算
- 比率:分子、补集(分母 − 分子)、分母任一小于阈值即抑制;只看分母不够,
  「30 人里 6.7% 被驳回」能反推出恰好 2 人。被抑制时用 side 说明是哪一头小,
  否则读的人分不清这个比率是接近 0 还是接近 1
- 统计量(分位数、合计、均值):样本量小于阈值即抑制
- 分布:逐格按计数规则抑制;恰好只有一格被抑制时再抑制剩下最小的一格,
  否则被抑制的那格可以用合计减出来
- 分布里的 0:fixed_keys 里的格子一定出(缺席按 0 补,0 同样被抑制)——对人的判定
  结果放这里,因为「某部门 0 人被采纳、被驳回的不足 5 人」合起来等于说这个部门
  看过的人全被驳回了,而不出格子就是明说 0。其余的格子为 0 时不出:它们是流程
  状态(待定、未决、未分配),「没有人还在待定」不指向任何人,硬要抑制反而会连带
  藏掉旁边的大数
- 分布的键:键里的文字和数字一样会进快照。只放行 ASCII 标识符形状的短键
  (渠道名、工具名、题组类别、数字 id);中文键分不清是部门名还是人名或一句
  原话,必须出自调用方给的白名单。其余并入 OTHER_KEY 一格
- 带文字的清单(主题名这类对多人行为的概括):文字没有白名单可对,换成四道条件
  ——计数与不同来源数都不小于阈值、是一个短语(不超长、不换行)、不含个人信息、
  不像指令。只看计数不够:一个人把同一句话问了八遍,计数够了,文字却还是这一个人
  的话。不够格的项整项不出,不留「被抑制」的格子:清单没有固定的全集,也不发布
  合计,少出一项推不出任何数;而留下格子就得留下文字,文字正是不该出的部分

不防的:跨指标的联合推断(两个不同的分布交叉相减、两个合计相减)。指标目录
不发布「部门 × 决定」这类交叉表的行合计,不给最要紧的那种减法留材料。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from official_agent.security.injection_guard import scan_injection
from official_agent.security.pii import mask_pii

#: 小于这个数的计数不出现在快照里。5 是统计披露控制里常用的下限,
#: 取值与 docs/proposals/cycle-retrospective.md D4 一致。
SMALL_CELL_MIN = 5

UNIT_COUNT = "count"
UNIT_RATIO = "ratio"
UNIT_MS = "ms"
UNIT_TOKENS = "tokens"

#: 被抑制的值落在哪一头:LOW = 计数(或比率的分子)小于阈值;
#: HIGH = 比率的补集小于阈值,即比率接近 1
SIDE_LOW = "low"
SIDE_HIGH = "high"

#: 不合规的分布键并入的那一格
OTHER_KEY = "other"

# 分布键的放行形状:ASCII 字母数字与少量连接符,至多 40 字符。渠道名、工具名、
# 题组类别、数字 id 都在这个范围里;带空格的自由文本和任何中文都不在。
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:\-]{1,40}$")

#: 清单项文字的长度上限:主题名是一个短语,更长的多半是照搬了某个人的原话
MAX_LABEL_CHARS = 40


@dataclass(frozen=True)
class RawCount:
    """一个计数。"""

    id: str
    value: int


@dataclass(frozen=True)
class RawRatio:
    """一个比率,带着分子分母来——抑制要看分子和补集,不能只收算好的小数。"""

    id: str
    numerator: int
    denominator: int


@dataclass(frozen=True)
class RawStat:
    """样本上的统计量(分位数、合计、均值);n 是参与统计的样本数。"""

    id: str
    value: float | None
    n: int
    unit: str


@dataclass(frozen=True)
class RawBreakdown:
    """一个合计按某个维度拆出的各格计数,格与格互斥。

    allowed_keys:额外放行的键(部门名这类中文标识);不在其中又不是 ASCII 标识符
    形状的键并入 OTHER_KEY。
    fixed_keys:无论有没有人都要出的格子,缺席按 0 补——放对人的判定结果
    (通过/不通过、采纳/驳回、各部门),理由见模块说明。fixed_keys 自动算在放行的
    键里。不在 fixed_keys 里的格子为 0 时不出。
    """

    id: str
    cells: Mapping[str, int]
    allowed_keys: frozenset[str] = frozenset()
    fixed_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RawRatioBreakdown:
    """按某个维度分别算的比率,每格是 (分子, 分母);各格的分母互不相干。"""

    id: str
    cells: Mapping[str, tuple[int, int]]


@dataclass(frozen=True)
class RawTextItem:
    """清单里的一项:一段概括多人行为的文字、它的计数、计数来自多少个不同的来源。

    sources 是不同来源的个数(会话数、评分卡数);group 是这一项的归类。
    """

    text: str
    count: int
    sources: int
    group: str | None = None


@dataclass(frozen=True)
class RawTextList:
    """按计数排出的前几项,每项带一段文字;各项之间不要求互斥。

    groups:放行的归类取值,不在其中的归类不带出去。limit:至多出几项。
    """

    id: str
    items: Sequence[RawTextItem]
    groups: frozenset[str] = frozenset()
    limit: int = 10


RawMetric = RawCount | RawRatio | RawStat | RawBreakdown | RawRatioBreakdown | RawTextList


@dataclass(frozen=True)
class MetricValue:
    """快照里的一个数。

    id 全快照唯一:标量就是指标 id,分布的一格是 ``指标id[键]``。
    suppressed 为真时 value 为 None;side 说明被抑制的值落在哪一头(SIDE_LOW 展示为
    「<5」,SIDE_HIGH 展示为「比分母少不到 5」),样本本身不足时 side 为 None。
    n 是样本量(计数的 n 就是它自己,比率的 n 是分母);n 小于阈值时也不给出。
    label 与 group 只有带文字的清单项才有:label 是那段文字,group 是它的归类。
    """

    id: str
    metric: str
    value: float | int | None
    unit: str
    n: int | None
    suppressed: bool = False
    side: str | None = None
    key: str | None = None
    label: str | None = None
    group: str | None = None


def suppress(raw: RawMetric) -> list[MetricValue]:
    """原始聚合 → 可以进快照的值(标量一条,分布每格一条)。"""
    if isinstance(raw, RawCount):
        return [_count_value(raw.id, raw.id, raw.value, key=None)]
    if isinstance(raw, RawRatio):
        return [_ratio_value(raw.id, raw.id, raw.numerator, raw.denominator, key=None)]
    if isinstance(raw, RawStat):
        return [_stat_value(raw)]
    if isinstance(raw, RawBreakdown):
        return _breakdown_values(raw)
    if isinstance(raw, RawTextList):
        return _text_list_values(raw)
    return [
        _ratio_value(cell_id(raw.id, key), raw.id, numerator, denominator, key=key)
        for key, (numerator, denominator) in sorted(_merge_ratio_cells(raw.cells).items())
    ]


def cell_id(metric_id: str, key: str) -> str:
    """分布里一格的 id。"""
    return f"{metric_id}[{key}]"


def _hidden_count(value_id: str, metric: str, *, key: str | None) -> MetricValue:
    return MetricValue(
        value_id, metric, None, UNIT_COUNT, None, suppressed=True, side=SIDE_LOW, key=key
    )


def _count_value(value_id: str, metric: str, value: int, *, key: str | None) -> MetricValue:
    if value < SMALL_CELL_MIN:
        return _hidden_count(value_id, metric, key=key)
    return MetricValue(value_id, metric, value, UNIT_COUNT, value, key=key)


def _ratio_value(
    value_id: str, metric: str, numerator: int, denominator: int, *, key: str | None
) -> MetricValue:
    if denominator < SMALL_CELL_MIN:
        return MetricValue(value_id, metric, None, UNIT_RATIO, None, suppressed=True, key=key)
    side = None
    if numerator < SMALL_CELL_MIN:
        side = SIDE_LOW
    elif denominator - numerator < SMALL_CELL_MIN:
        side = SIDE_HIGH
    if side is not None:
        return MetricValue(
            value_id, metric, None, UNIT_RATIO, denominator, suppressed=True, side=side, key=key
        )
    # 4 位小数:足够区分 0.01 个百分点,又不把浮点噪声写进长期保存的快照
    return MetricValue(
        value_id, metric, round(numerator / denominator, 4), UNIT_RATIO, denominator, key=key
    )


def _stat_value(raw: RawStat) -> MetricValue:
    if raw.n < SMALL_CELL_MIN or raw.value is None:
        return MetricValue(raw.id, raw.id, None, raw.unit, None, suppressed=True)
    return MetricValue(raw.id, raw.id, raw.value, raw.unit, raw.n)


def _safe_key(key: str, allowed: frozenset[str] = frozenset()) -> str:
    """键合规则原样返回,否则归到 OTHER_KEY。"""
    text = str(key)
    if text in allowed:
        return text
    # mask_pii 兜住形状合规的纯数字:手机号、学号也是「字母数字」
    if _SAFE_KEY.match(text) and mask_pii(text) == text:
        return text
    return OTHER_KEY


def _merge_ratio_cells(cells: Mapping[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
    merged: dict[str, tuple[int, int]] = {}
    for key, (numerator, denominator) in cells.items():
        safe = _safe_key(key)
        prev_numerator, prev_denominator = merged.get(safe, (0, 0))
        merged[safe] = (prev_numerator + int(numerator), prev_denominator + int(denominator))
    return merged


def _breakdown_cells(raw: RawBreakdown) -> dict[str, int]:
    """要出的格子:键归一后合并,固定格补 0,其余为 0 的格子去掉。"""
    allowed = raw.allowed_keys | raw.fixed_keys
    cells = {key: 0 for key in raw.fixed_keys}
    for key, count in raw.cells.items():
        safe = _safe_key(key, allowed)
        cells[safe] = cells.get(safe, 0) + int(count)
    return {key: count for key, count in cells.items() if count or key in raw.fixed_keys}


def _breakdown_values(raw: RawBreakdown) -> list[MetricValue]:
    cells = _breakdown_cells(raw)
    hidden = {key for key, count in cells.items() if count < SMALL_CELL_MIN}
    visible = sorted((count, key) for key, count in cells.items() if key not in hidden)
    if len(hidden) == 1 and visible:
        # 只藏一格等于没藏:合计减去其余各格就是它。再藏最小的一格,
        # 两格只能推出合在一起的数
        hidden.add(visible[0][1])
    return [
        _hidden_count(cell_id(raw.id, key), raw.id, key=key)
        if key in hidden
        else _count_value(cell_id(raw.id, key), raw.id, count, key=key)
        for key, count in sorted(cells.items())
    ]


def _safe_label(text: str) -> bool:
    """这段文字能不能进快照:是一个短语,不含个人信息,不像指令。"""
    label = text.strip()
    if not label or len(label) > MAX_LABEL_CHARS:
        return False
    # 控制字符(含换行、制表)一律不收:短语里不该有,有了多半是夹带
    if any(unicodedata.category(ch).startswith("C") for ch in label):
        return False
    if mask_pii(label) != label:
        return False
    hit, _ = scan_injection(label)
    return not hit


def _text_list_values(raw: RawTextList) -> list[MetricValue]:
    """清单 → 够格的各项,按计数从大到小编号(t01、t02……),至多 limit 项。"""
    kept = [
        item
        for item in raw.items
        if item.count >= SMALL_CELL_MIN
        and item.sources >= SMALL_CELL_MIN
        and _safe_label(item.text)
    ]
    # 稳定排序:计数相同的保持调用方给的先后,同样的输入出同样的编号
    kept.sort(key=lambda item: -item.count)
    values: list[MetricValue] = []
    for index, item in enumerate(kept[: max(raw.limit, 0)], start=1):
        key = f"t{index:02d}"
        values.append(
            MetricValue(
                cell_id(raw.id, key),
                raw.id,
                int(item.count),
                UNIT_COUNT,
                int(item.count),
                key=key,
                label=item.text.strip(),
                group=item.group if item.group in raw.groups else None,
            )
        )
    return values
