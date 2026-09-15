"""知识库检索工具(RAG #134 R3):search_knowledge——客服的社团/部门/FAQ 只读检索。

docstring 契约(ADR-0003):写「何时用」+ 边界,docstring 即模型看到的工具描述。

降级契约(#120):0 命中/服务不可用/EMBED 未配置都不抛异常——返回结构化
状态由模型转述(查不到明说/引导官网),检索故障不阻塞对话。
引用锚是 source_id+title(条目级,#118);snippet 截断只供作答参考。
"""

from typing import Any

from official_agent.kb import store as kb_store
from official_agent.kb.embedding import EmbeddingError

_TOP_K = 4
_SNIPPET_CHARS = 200

_UNAVAILABLE = {
    "status": "unavailable",
    "message": "知识库检索暂不可用;如实告知用户,并建议查看官网或报名说明,不要编造",
}


async def search_knowledge(query: str) -> dict[str, Any]:
    """查询社团官方知识库(社团介绍/部门职能/招新流程/常见问题)后作答。

    何时用:用户问「社团有哪些部门/某部门做什么/怎么加入/面试流程/
    协会活动/报名常见问题」等社团公开信息时,先查这里再回答;
    回答时注明信息来自哪条条目(用 title,如「据知识库《技术部介绍》」)。

    边界:只覆盖管理面板录入的官方知识;返回 status=not_found 时明确说
    「知识库里没有这部分,建议查看官网或报名说明」,绝不凭常识编造社团
    细节;status=unavailable 时如实说明检索暂不可用并引导官网。
    这里没有候选人个人数据(个人进度/面试安排走其他工具)。

    示例:search_knowledge(query="技术部主要负责什么")
    """
    try:
        hits = await kb_store.search(query, top_k=_TOP_K)
    except EmbeddingError:
        return dict(_UNAVAILABLE)
    except Exception:  # noqa: BLE001 — 检索失败同降级,不阻塞对话(#120)
        return dict(_UNAVAILABLE)
    if not hits:
        return {
            "status": "not_found",
            "message": "知识库中没有相关内容;明确告知用户并建议查看官网/报名说明",
            "results": [],
        }
    return {
        "status": "ok",
        "results": [
            {
                "source_id": h.source_id,
                "title": h.title,
                "heading": h.heading_path or None,
                "snippet": h.chunk_text[:_SNIPPET_CHARS],
            }
            for h in hits
        ],
    }
