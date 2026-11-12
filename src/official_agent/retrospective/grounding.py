"""数字落地守卫:模型写的每句话里的数,都要能在这句话引用的指标里找到。纯函数,零 IO。

报告里的数字全部由代码算(docs/proposals/cycle-retrospective.md D1),模型只解读。
守卫把一句话里的数字逐个抠出来,和它引用的那几个指标的值、样本量、基线值、差值
比对;对不上的句子先交回撰稿人改一次,还对不上就删掉并记下来。

比对的是「引用的指标」而不是整份快照:快照里有几百个数,任何一个编出来的数都
可能碰巧和某个不相干的指标相等。

对得上的写法:
- 计数、毫秒、token、样本量:照抄,或写成带「万 / 亿 / 千」的约数(按写出的精度
  四舍五入后相等才算)
- 比率与相对变化:百分比(按写出的小数位四舍五入)、两位以上的小数、「几成」
- 比率的差:只认「个百分点」——写成百分比分不清是相对变化还是百分点差

守卫看不懂的量一律按对不上处理:「三成」「翻了一倍」「过半」这类模糊量词,以及
「两人」「三名」这类中文数字的人数——后者正是「某部门只有两个人被驳回」换个写法。
这部分是启发式的,宁可多删一句无害的话。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from official_agent.retrospective.guard import SMALL_CELL_MIN, UNIT_RATIO

KIND_PLAIN = "plain"
KIND_RATIO = "ratio"
KIND_POINTS = "points"


@dataclass(frozen=True)
class Allowed:
    """一个可以出现在文字里的数;kind 决定它能怎么写(见模块说明)。"""

    value: float
    kind: str


@dataclass(frozen=True)
class Violation:
    """一句对不上的话,连同里面对不上的数字或模糊量词(原样摘出)。"""

    sentence: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class GroundingContext:
    """与具体指标无关的放行项。

    always:随处可写的数(采集参数)。
    strip:比对之前先从文字里拿掉的片段——指标 id、清单项的文字(主题名)、周期编号、
    时间窗日期、年份、分位数标记、「不足 5」这类抑制用语。它们带数字,但不是报告在
    陈述的量。
    """

    always: tuple[Allowed, ...]
    strip: tuple[re.Pattern[str], ...]


# 数字字面量。前面不能紧挨字母、下划线、小数点、数字或方括号——那是标识符、
# 版本号或指标 id 的一部分(P90、v2、funnel.assigned_by_dept[3])。
_LITERAL = re.compile(
    r"(?<![A-Za-z_\d.\[])"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"\s*(?P<suffix>个百分点|百分点|%|成|百万|万|亿|千|[kK](?![A-Za-z])|M(?![A-Za-z]))?"
)

_PERCENT_SUFFIX = "%"
_POINT_SUFFIXES = frozenset({"个百分点", "百分点"})
_TENTHS_SUFFIX = "成"
_SCALES = {"千": 1e3, "k": 1e3, "K": 1e3, "万": 1e4, "百万": 1e6, "M": 1e6, "亿": 1e8}

_CN_NUMERAL = "一二两三四五六七八九十百千万几数半"
# 「成」后面跟这些字时是普通词(成为、成员、半成品),不是「几成」
_NOT_TENTHS = "为绩员功果本立熟长都分交型效因见就品"
_FUZZY = re.compile(
    rf"[{_CN_NUMERAL}]+\s*成(?![{_NOT_TENTHS}])"
    rf"|[{_CN_NUMERAL}]+\s*倍|翻倍|翻番|翻了?一番"
    rf"|[{_CN_NUMERAL}]+分之[{_CN_NUMERAL}]+"
    r"|一半|过半|近半|超半|半数|大半"
    # 中文数字的人数;「每一位」「第一名」这类泛指与序数不算
    r"|(?<![每某任同另这那哪第])[一二两三四五六七八九十百千几余]+\s*多?个?(?:人|名|位)(?![员称字置数次])"
)

# 句末标点,半角与全角(\uff01 \uff1f \uff1b 是全角的感叹号、问号、分号)都算
_SENTENCE_END = re.compile(r"(?<=[。!?;\n\uff01\uff1f\uff1b])")

# 抑制用语:快照把小于阈值的计数写成「不足 5」,报告照这么说是对的
_SUPPRESSED_PHRASE = re.compile(
    rf"(?:<|不足|少于|小于|不到|低于)\s*{SMALL_CELL_MIN}(?![\d.])"
)
_YEAR = re.compile(r"20\d{2}\s*(?:年|届|级|学年)")
_PERCENTILE = re.compile(r"第?\s*(?:50|90|99)\s*百?分位")
_LIST_NUMBER = re.compile(r"(?m)^\s*\d+\s*[.、)]\s*")


def context_for(snapshot: dict[str, Any]) -> GroundingContext:
    """从快照正文得到放行项。"""
    strip: list[re.Pattern[str]] = []
    ids = sorted({m["id"] for m in snapshot.get("metrics", [])}, key=len, reverse=True)
    ids += sorted({e["id"] for e in snapshot.get("catalog", [])}, key=len, reverse=True)
    if ids:
        strip.append(re.compile("|".join(re.escape(i) for i in ids)))
    # 主题名里的数字与「三人」这类写法是名字的一部分:照抄主题名不算陈述一个量。
    # 只认一字不差的照抄——改写过的主题名里的数照常要对得上
    labels = sorted(
        {m["label"] for m in snapshot.get("metrics", []) if m.get("label")}, key=len, reverse=True
    )
    if labels:
        strip.append(re.compile("|".join(re.escape(label) for label in labels)))
    cycle_ids = [snapshot.get("cycle_id"), (snapshot.get("baseline") or {}).get("cycle_id")]
    for cycle_id in cycle_ids:
        if cycle_id is not None:
            strip.append(re.compile(rf"周期\s*#?\s*{int(cycle_id)}(?![\d.])"))
    strip.extend(_window_date_patterns(snapshot.get("window")))
    strip.extend([_SUPPRESSED_PHRASE, _YEAR, _PERCENTILE, _LIST_NUMBER])
    always = tuple(
        Allowed(float(value), KIND_PLAIN)
        for value in (snapshot.get("params") or {}).values()
        if isinstance(value, int | float) and not isinstance(value, bool)
    )
    return GroundingContext(always=always, strip=tuple(strip))


def _window_date_patterns(window: dict[str, Any] | None) -> list[re.Pattern[str]]:
    """时间窗首尾两天的几种写法。别的日期快照里没有依据,不放行。"""
    if not window:
        return []
    first = datetime.fromisoformat(window["start"]).date()
    # 时间窗右端不含,报告里说的「结束日」是它的前一天
    last = datetime.fromisoformat(window["end"]).date() - timedelta(days=1)
    return [p for day in (first, last) for p in _date_patterns(day)]


def _date_patterns(day: date) -> list[re.Pattern[str]]:
    return [
        re.compile(rf"{day.year}-0?{day.month}-0?{day.day}(?!\d)"),
        re.compile(rf"(?:{day.year}\s*年\s*)?0?{day.month}\s*月\s*0?{day.day}\s*[日号]"),
    ]


def allowed_from(entries: list[dict[str, Any]]) -> list[Allowed]:
    """被引用的指标值 → 这句话里可以出现的数(值、样本量、基线值、差值)。"""
    allowed: list[Allowed] = []
    for entry in entries:
        kind = KIND_RATIO if entry.get("unit") == UNIT_RATIO else KIND_PLAIN
        for key in ("value", "prev"):
            if entry.get(key) is not None:
                allowed.append(Allowed(float(entry[key]), kind))
        if entry.get("n") is not None:
            allowed.append(Allowed(float(entry["n"]), KIND_PLAIN))
        if entry.get("delta") is not None:
            delta_kind = KIND_POINTS if kind == KIND_RATIO else KIND_PLAIN
            allowed.append(Allowed(abs(float(entry["delta"])), delta_kind))
        if entry.get("delta_ratio") is not None:
            allowed.append(Allowed(abs(float(entry["delta_ratio"])), KIND_RATIO))
    return allowed


def check_text(
    text: str, allowed: list[Allowed], context: GroundingContext
) -> list[Violation]:
    """逐句检查,返回对不上的句子。"""
    candidates = [*allowed, *context.always]
    violations: list[Violation] = []
    for sentence in _SENTENCE_END.split(text):
        if not sentence.strip():
            continue
        tokens = _ungrounded_tokens(sentence, candidates, context)
        if tokens:
            violations.append(Violation(sentence.strip(), tuple(tokens)))
    return violations


def remove_sentences(text: str, violations: list[Violation]) -> str:
    """把对不上的句子从文字里删掉,其余原样保留。"""
    doomed = {v.sentence for v in violations}
    kept = [s for s in _SENTENCE_END.split(text) if s.strip() and s.strip() not in doomed]
    return "".join(kept).strip()


def separate_thousands(text: str) -> str:
    """把文字里五位及以上的整数写成带千分位的形式(「9634112」→「9,634,112」)。

    只改写法不改数。给已经过了守卫的文字用:出口的脱敏规则把五位以上的连续数字
    当成 QQ 号或学号,合规的大数不整理一下会被它抹掉。
    """

    def _rewrite(match: re.Match[str]) -> str:
        digits = match.group("num")
        if "," in digits or len(digits) < 5:
            return match.group(0)
        return match.group(0).replace(digits, f"{int(digits):,}", 1)

    return _LITERAL.sub(_rewrite, text)


def _ungrounded_tokens(
    sentence: str, candidates: list[Allowed], context: GroundingContext
) -> list[str]:
    # 全角的数字、百分号、括号先归一成半角:模型写中文时两种都会用
    cleaned = unicodedata.normalize("NFKC", sentence)
    for pattern in context.strip:
        cleaned = pattern.sub(" ", cleaned)
    tokens = [m.group(0).strip() for m in _FUZZY.finditer(cleaned)]
    for match in _LITERAL.finditer(cleaned):
        if not any(_matches(match, candidate) for candidate in candidates):
            tokens.append(match.group(0).strip())
    return tokens


def _matches(literal: re.Match[str], allowed: Allowed) -> bool:
    number = float(literal.group("num").replace(",", "") + (literal.group("frac") or ""))
    decimals = len(literal.group("frac") or ".") - 1
    suffix = literal.group("suffix") or ""
    # 写出几位小数,就按那一位四舍五入来比:「25.3%」对得上 0.2534,对不上 0.2561
    tolerance = 0.5 * 10 ** (-decimals) + 1e-9
    if suffix == _PERCENT_SUFFIX:
        return allowed.kind == KIND_RATIO and abs(allowed.value * 100 - number) <= tolerance
    if suffix in _POINT_SUFFIXES:
        return allowed.kind == KIND_POINTS and abs(allowed.value * 100 - number) <= tolerance
    if suffix == _TENTHS_SUFFIX:
        return allowed.kind == KIND_RATIO and abs(allowed.value * 10 - number) <= tolerance
    scale = _SCALES.get(suffix, 1.0)
    if allowed.kind == KIND_PLAIN:
        if scale == 1.0 and decimals == 0:
            # 不带单位的整数必须一字不差:约数只能靠「万 / 亿」或小数位来表达,
            # 否则「0」会和 0.45 这样的参数「四舍五入后相等」
            return abs(allowed.value - number) < 1e-9
        return abs(allowed.value - number * scale) <= tolerance * scale
    # 比率也可以写成小数,但至少两位:「0」「1」「0.3」会和一大片比率对上
    return (
        allowed.kind == KIND_RATIO
        and scale == 1.0
        and decimals >= 2
        and abs(allowed.value - number) <= tolerance
    )
