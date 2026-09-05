"""Prompt 加载器(ADR-0004):prompt 唯一权威是 prompts/ 文件,代码零 prompt 字符串。

一图一节点一文件 + frontmatter(version/model_tier 等);改 prompt 必跑
钉住该版本的 eval。
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(filename: str) -> str:
    """读 prompts/<filename>,剥离 frontmatter,返回正文(同 assistant 先例)。"""
    text = (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
    if text.startswith("---"):
        _, _, body = text.split("---", 2)
        return body.strip()
    return text.strip()
