"""Embedding 客户端(RAG #134):OpenAI-compatible /embeddings。

独立 EMBED_* 配置组(config.py),与对话模型 build_model 平行:
不塞 llm_base_url、不入 HOT_KEYS(SEC-01:密钥只在 env)。
Anthropic 无 embedding → 托管中文模型(Qwen3-Embedding-0.6B/BGE-M3 等);
端点与维度落在 kb_meta(schema.py),换模型 = 全量 reindex。
"""

from collections.abc import Awaitable, Callable

import httpx

from official_agent.config import get_settings

# async callable: texts -> vectors(测试/换端点注入点)
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class EmbeddingError(RuntimeError):
    """embedding 端点调用失败(HTTP 非 2xx/结构不符/维度不符)。"""


class EmbeddingNotConfiguredError(EmbeddingError):
    """EMBED_* 未配置——检查点①:需用户提供托管 embedding 端点。"""


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量嵌入,返回与输入等长且保序的向量列表。

    保序按响应里的 index 字段重排(不信任 provider 的返回顺序)。
    EMBED_DIM>0 时对返回维度强校验,错配立即抛错(禁脏向量入库)。
    """
    settings = get_settings()
    if not (settings.embed_base_url and settings.embed_api_key and settings.embed_model):
        raise EmbeddingNotConfiguredError(
            "embedding 未配置:需在 .env 设 EMBED_BASE_URL/EMBED_API_KEY/EMBED_MODEL"
            "(见 .env.example;当前 LLM 代理无 embedding 模型,night-run 执行板 #136 检查点①)"
        )
    base = settings.embed_base_url.rstrip("/")
    url = base if base.endswith("/embeddings") else f"{base}/embeddings"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.embed_api_key}"},
                json={"model": settings.embed_model, "input": texts},
            )
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"embedding 端点连不上:{exc}") from exc
    if resp.status_code != 200:
        raise EmbeddingError(f"embedding 端点 HTTP {resp.status_code}:{resp.text[:200]}")
    items = resp.json().get("data")
    if not isinstance(items, list) or len(items) != len(texts):
        raise EmbeddingError(f"embedding 响应条数不符:期望 {len(texts)}")
    vectors: list[list[float] | None] = [None] * len(texts)
    for item in items:
        index = item.get("index")
        if not isinstance(index, int) or not 0 <= index < len(texts):
            raise EmbeddingError(f"embedding 响应 index 非法:{index!r}")
        vectors[index] = item["embedding"]
    typed: list[list[float]] = []
    for v in vectors:
        if v is None:
            raise EmbeddingError("embedding 响应缺 index 条目")
        typed.append(v)
    if settings.embed_dim and len(typed[0]) != settings.embed_dim:
        raise EmbeddingError(
            f"维度不符:端点返回 {len(typed[0])},EMBED_DIM={settings.embed_dim}"
            "——改 EMBED_DIM 为端点真实维度,且已有库需全量 reindex"
        )
    return typed
