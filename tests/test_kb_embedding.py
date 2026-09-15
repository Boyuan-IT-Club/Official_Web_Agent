"""RAG #134 R1:embedding 客户端单测(respx 拦 HTTP,不连真实端点)。"""


import httpx
import pytest
import respx
from httpx import Response

from official_agent.config import Settings
from official_agent.kb.embedding import (
    EmbeddingError,
    EmbeddingNotConfiguredError,
    embed_texts,
)

_URL = "http://emb.test/v1/embeddings"


def _configured(monkeypatch, **over):
    base = {
        "embed_base_url": "http://emb.test/v1",
        "embed_api_key": "test-key",
        "embed_model": "test-embed",
        "embed_dim": 0,
    }
    base.update(over)
    s = Settings(_env_file=None, **base)
    monkeypatch.setattr("official_agent.kb.embedding.get_settings", lambda: s)


@respx.mock
async def test_embed_texts_posts_model_and_input(monkeypatch) -> None:
    _configured(monkeypatch)
    route = respx.post(_URL).mock(
        return_value=Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    )
    vectors = await embed_texts(["你好"])
    assert vectors == [[0.1, 0.2]]
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "test-embed"
    assert body["input"] == ["你好"]
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


@respx.mock
async def test_embed_texts_reorders_by_index(monkeypatch) -> None:
    """保序按响应 index 重排,不信任 provider 返回顺序。"""
    _configured(monkeypatch)
    respx.post(_URL).mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [3.0, 4.0]},
                    {"index": 0, "embedding": [1.0, 2.0]},
                ]
            },
        )
    )
    vectors = await embed_texts(["first", "second"])
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]


async def test_not_configured_raises(monkeypatch) -> None:
    s = Settings(_env_file=None)
    monkeypatch.setattr("official_agent.kb.embedding.get_settings", lambda: s)
    with pytest.raises(EmbeddingNotConfiguredError):
        await embed_texts(["x"])


@respx.mock
async def test_http_500_raises(monkeypatch) -> None:
    _configured(monkeypatch)
    respx.post(_URL).mock(return_value=Response(500, text="boom"))
    with pytest.raises(EmbeddingError, match="HTTP 500"):
        await embed_texts(["x"])


@respx.mock
async def test_connection_error_raises(monkeypatch) -> None:
    _configured(monkeypatch)
    respx.post(_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(EmbeddingError, match="连不上"):
        await embed_texts(["x"])


@respx.mock
async def test_dim_mismatch_raises(monkeypatch) -> None:
    _configured(monkeypatch, embed_dim=3)
    respx.post(_URL).mock(
        return_value=Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
    )
    with pytest.raises(EmbeddingError, match="维度不符"):
        await embed_texts(["x"])


@respx.mock
async def test_bad_index_raises(monkeypatch) -> None:
    _configured(monkeypatch)
    respx.post(_URL).mock(
        return_value=Response(200, json={"data": [{"index": 5, "embedding": [1.0]}]})
    )
    with pytest.raises(EmbeddingError, match="index 非法"):
        await embed_texts(["x"])
