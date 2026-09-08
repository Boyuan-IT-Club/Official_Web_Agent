"""KB 结构分块(RAG #134;选型依据 docs/research/rag-kb-survey.md,#119 拍板)。

- FAQ 对 = 1 chunk(Q/A 拼接);自由正文按 Markdown 标题层级切
- 超长段落按句末标点细分,单 chunk 硬上限 MAX_CHUNK_CHARS
- chunk 携带 heading_path(标题链);引用粒度条目级,块只影响检索粒度
- 纯函数,无 IO——入库与检索两侧共用,行为变化即召回变化
"""

import re
from dataclasses import dataclass

MAX_CHUNK_CHARS = 800

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# 句末标点后切(中英文);后行断言保住标点
_SENTENCE = re.compile(r"(?<=[。！？!?.;；])\s*")


@dataclass(frozen=True)
class KbChunk:
    """一个可嵌入块。ordinal 在单条 source 内从 0 连续递增。"""

    ordinal: int
    text: str
    heading_path: str


def split_faq(question: str, answer: str) -> list[KbChunk]:
    """FAQ 对整条一块:问/答拼接保证「问题词」进同一向量。"""
    text = f"Q: {question.strip()}\nA: {answer.strip()}"
    return [KbChunk(ordinal=0, text=text, heading_path="")]


def split_doc(content_md: str) -> list[KbChunk]:
    """自由正文分块:标题换节,段落聚合到上限,超长段硬切。

    标题行不进 chunk 文本(它活在 heading_path 里);空行只作段落分隔,
    聚合与否由长度决定。
    """
    stack: list[tuple[int, str]] = []
    chunks: list[KbChunk] = []
    heading_path = ""
    parts: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal parts, length
        text = "\n".join(parts).strip()
        if text:
            chunks.append(KbChunk(ordinal=len(chunks), text=text, heading_path=heading_path))
        parts, length = [], 0

    for raw in content_md.splitlines():
        line = raw.rstrip()
        heading = _HEADING.match(line)
        if heading:
            flush()
            level, title = len(heading.group(1)), heading.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            heading_path = " > ".join(t for _, t in stack)
            continue
        if not line.strip():
            continue
        for piece in _hard_split(line):
            if length + len(piece) + 1 > MAX_CHUNK_CHARS and parts:
                flush()
            parts.append(piece)
            length += len(piece) + 1
    flush()
    return chunks


def embed_text(chunk: KbChunk) -> str:
    """送入向量模型的文本:标题链拼在正文前,补回节标题的语义。"""
    if chunk.heading_path:
        return f"{chunk.heading_path}\n{chunk.text}"
    return chunk.text


def _hard_split(text: str, cap: int = MAX_CHUNK_CHARS) -> list[str]:
    """单行超上限时按句切;单句仍超长则按字符硬断。"""
    if len(text) <= cap:
        return [text]
    pieces: list[str] = []
    cur = ""
    for sentence in _SENTENCE.split(text):
        if not sentence:
            continue
        if cur and len(cur) + len(sentence) + 1 > cap:
            pieces.append(cur)
            cur = ""
        while len(sentence) > cap:  # 无标点超长行兜底
            pieces.append(sentence[:cap])
            sentence = sentence[cap:]
        cur = f"{cur} {sentence}".strip() if cur else sentence
    if cur:
        pieces.append(cur)
    return pieces
