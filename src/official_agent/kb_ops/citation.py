"""引文复核:模型声称「原文里有」的片段,由代码回到原文里核对。纯函数。

判据是逐字包含,只放过排版层面的差异(空白、全半角、标点)。不做近似匹配:这里的
引文要支撑一句会发布到知识库的话,宁可因为模型改了一个字而打回,也不放过一段原文
里没有的话。评分子图的引文校验容忍带叙述的转述,判据更松,所以不共用。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

#: 引文归一化后的最短长度。太短的片段(「社团」「可以」)几乎在任何块里都能找到,
#: 证明不了什么
MIN_QUOTE_CHARS = 6
#: 省略号两侧各算一段;每一段也要有起码的长度
_MIN_FRAGMENT_CHARS = 3
_ELLIPSIS_RE = re.compile(r"…+|\.{3,}")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def normalize(text: str) -> str:
    """只留文字与数字:全半角统一、英文转小写,去掉空白、标点与符号。"""
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if unicodedata.category(ch)[0] in ("L", "N"))


def quote_in_text(quote: str, text: str) -> bool:
    """引文是否逐字出现在原文里。

    引文里的省略号表示中间略去了内容:各段都必须在原文里出现,且先后顺序一致。
    引文太短、或任何一段找不到,都返回 False。
    """
    fragments = [normalize(part) for part in _ELLIPSIS_RE.split(quote)]
    fragments = [f for f in fragments if f]
    if not fragments or sum(len(f) for f in fragments) < MIN_QUOTE_CHARS:
        return False
    if any(len(f) < _MIN_FRAGMENT_CHARS for f in fragments):
        return False
    haystack = normalize(text)
    position = 0
    for fragment in fragments:
        found = haystack.find(fragment, position)
        if found < 0:
            return False
        position = found + len(fragment)
    return True


def ungrounded_numbers(sentence: str, text: str) -> Sequence[str]:
    """句子里出现、而原文里没有的数字(金额、日期、时长、人数……)。

    引文复核只能证明「引的那段原文存在」,证明不了句子没有比原文多说什么。数字是
    最容易被塞进来、也最伤人的那一类多出来的信息(「入社要交 500 元」),所以单独
    卡一道:句子里的每个阿拉伯数字都必须在原文里原样出现。原文写「三十分钟」而
    句子写「30 分钟」也会被卡住——宁可打回,让起草者照原文的写法写。
    """
    source_numbers = set(_NUMBER_RE.findall(unicodedata.normalize("NFKC", text)))
    found = _NUMBER_RE.findall(unicodedata.normalize("NFKC", sentence))
    return [n for n in dict.fromkeys(found) if n not in source_numbers]
