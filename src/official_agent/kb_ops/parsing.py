"""模型回复里的 JSON 抽取:知识库运营各角色共用。"""

from __future__ import annotations

import json
from typing import Any


def first_json_object(text: str) -> Any:
    """回复里第一个完整的 JSON 对象;前后的代码围栏与杂文忽略。

    没有对象或解析不了抛 ValueError(调用方据此带着诊断让模型重写)。
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("回复里没有 JSON 对象")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败:{exc.msg}") from exc
    return obj
