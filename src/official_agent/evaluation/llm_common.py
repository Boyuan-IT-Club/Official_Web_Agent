"""评估线共享的 LLM 调用小件:回复解包 / prompt 版本 / 温度定档 / 校验回灌。

四个评估模块(graph/investigate/bundle/judge/tech_stack)曾各自内联这四样,
同一知识多个出处;本模块是唯一权威出处。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage

from official_agent.state.conversation import usage_from_response

# 温度按用途定档:评分要稳定可复现(0.1);出题允许一点发散换覆盖面(0.2)。
# 同值同义,改这里一处生效;调用点不要再写字面量。
TEMPERATURE_SCORING = 0.1
TEMPERATURE_GENERATION = 0.2


def content_text(response: Any) -> str:
    """模型回复 → 文本(多模态 content 取文本块拼接)。"""
    raw = getattr(response, "content", "")
    if isinstance(raw, list):
        raw = "".join(block.get("text", "") for block in raw if isinstance(block, dict))
    return raw if isinstance(raw, str) else str(raw)


def prompt_version(prompt_file: str) -> str:
    """prompt frontmatter 的 version(ADR-0004:文件是唯一权威)。

    信封落盘的版本锚必须与**本次实际使用**的 prompt 一致:每个调用点的
    PROMPT_FILE 常量即其真实输入,不要手抄版本号。
    """
    from official_agent.prompt_loader import load_prompt_meta

    return load_prompt_meta(prompt_file).get("version", "unknown")


async def invoke_with_retry[T](
    model: Any,
    prompt_text: str,
    *,
    parse: Callable[[str], T],
    build_corrective: Callable[[ValueError], str],
    fail_message: str,
    config: dict | None = None,
    attempts: int = 2,
) -> tuple[T, dict[str, int | None] | None]:
    """带校验回灌的 LLM 调用:输出不合规(ValueError)时把诊断回灌重试。

    - parse:回复文本 → 结果;不合规抛 ValueError(pydantic ValidationError
      是其子类,schema 校验与业务校验同路)
    - build_corrective:校验诊断 → 纠正段文本(追加到 prompt 重发;诊断内嵌
      模型产出、与简历同源可能含注入 payload,纠正段在数据区之外,须自行包
      数据区——标签内一律是数据)
    - 只捕 ValueError:程序缺陷必须向上暴露,不能伪装成模型不合规
    返回 (parse 结果, 最后一次带 usage 的响应用量;从未采到则 None)。
    超过次数仍不合规 → ValueError(f"{fail_message}:{最后一次诊断}")。
    """
    corrective = ""
    usage: dict[str, int | None] | None = None
    last_err: ValueError | None = None
    for _attempt in range(attempts):
        if config is None:
            resp = await model.ainvoke([HumanMessage(content=prompt_text + corrective)])
        else:
            resp = await model.ainvoke(
                [HumanMessage(content=prompt_text + corrective)], config=config
            )
        got = usage_from_response(resp)
        if got is not None:
            usage = got
        try:
            return parse(content_text(resp)), usage
        except ValueError as exc:
            last_err = exc
            corrective = build_corrective(exc)
    raise ValueError(f"{fail_message}:{last_err}")
