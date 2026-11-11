"""落地校验:模型声称「原文里有」的文本,回到原文里核对。纯函数,零 IO。

评委的引文、批判者指出的注入片段和漏判证据都走这里:编造的原文在简历里找不到。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

#: 引号包裹的片段:模型常把原文放进引号,引号外是自己的交代。成对的中英引号都收。
_QUOTED_RE = re.compile(
    "[\u201c\u0022\u300c\u300e]([^\u201d\u0022\u300d\u300f]+)[\u201d\u0022\u300d\u300f]"
)

#: 两段文本算「重叠」所需的最短公共片段。再短就容易撞上社团名、常用词这类
#: 两边都会出现的字样;初值,没有用真实数据校准过。
_MIN_OVERLAP_CHARS = 8


def squeeze(text: str) -> str:
    """去掉全部空白:换行与缩进不该影响「是不是同一段话」。"""
    return "".join(text.split())


def evidence_in(evidence: str, source: str) -> bool:
    """依据是否落在原文:直接引述、近似引述,或**带叙述的引述**。

    模型写依据的形态有三种,都要放行:
    1. 裸引述:整句是原文(允许一字压缩:「大一起接触」→「大一接触」);
    2. 拼装引述:若干原文片段用标点串起来(「『片段一』;『片段二』」);
    3. **带叙述的引述**:模型用自己的话交代出处,把原文放在引号里
       (「项目经验栏写“运营过 2w 粉账号…”」)——引号外是模型的话,
       引号内才是它声称的证据。

    只认第 1 种会误杀后两种:实测真实简历上 3/4 份整份无分。但也不能松到
    「随便挑一句像原文的就放行」——那等于取消这道闸门,编造照样能过。
    故判据是:**凡是引号里的内容,必须逐段落得到原文**;引号外的叙述不计。
    没有引号时,退化为整句近似匹配 + 标点切段。
    """
    ev, src = squeeze(evidence), squeeze(source)
    if not ev:
        return False
    if ev in src:
        return True

    def _fuzzy(segment: str) -> bool:
        """一段文本是否忠实出自原文(允许掉字/标点差异)。"""
        if not segment:
            return False
        if segment in src:
            return True
        matcher = SequenceMatcher(None, segment, src, autojunk=False)
        covered = sum(b.size for b in matcher.get_matching_blocks())
        return covered >= max(6, int(len(segment) * 0.7))

    # 形态 3:有引号 → 引号内的每一段都必须是原文(这是模型声称的证据本体)
    quoted = [squeeze(q) for q in _QUOTED_RE.findall(evidence or "")]
    quoted = [q for q in quoted if len(q) >= 4]
    if quoted:
        return all(_fuzzy(q) for q in quoted)

    # 形态 2:无引号的拼装 —— 按标点切段,各段落得到原文且覆盖率够高
    pieces = [p for p in re.split(r"[;；,、。!?…—\-—()()【】\[\]:]", ev) if len(p) >= 6]
    if len(pieces) >= 2:
        hit = sum(len(p) for p in pieces if _fuzzy(p))
        return hit >= int(len(ev) * 0.8)

    # 形态 1:整句近似引述
    return _fuzzy(ev)


def spans_overlap(first: str, second: str) -> bool:
    """两段文本是否有实质重叠(忽略空白):一段包含另一段,或最长公共片段够长。

    「够长」= 不短于较短那段的一半,且不短于 _MIN_OVERLAP_CHARS。这是确定性的字符串
    判断,不看语义。
    """
    shorter, longer = sorted((squeeze(first), squeeze(second)), key=len)
    if not shorter:
        return False
    if shorter in longer:
        return True
    matcher = SequenceMatcher(None, shorter, longer, autojunk=False)
    common = matcher.find_longest_match(0, len(shorter), 0, len(longer)).size
    return common >= max(_MIN_OVERLAP_CHARS, len(shorter) // 2)
