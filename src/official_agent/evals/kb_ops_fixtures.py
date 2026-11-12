"""知识库运营各 eval 执行器共用的夹具装配:yaml → 原文块 / 草稿 / 假的知识库读取。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from official_agent.kb.store import ChunkRecord, SearchHit
from official_agent.kb_ops.draft import DraftProposal


def chunk_records(spec: dict[str, Any]) -> dict[str, ChunkRecord]:
    """{chunk_id: {source_id, title, type, text}} → {chunk_id: ChunkRecord}。"""
    return {
        chunk_id: ChunkRecord(
            chunk_id=chunk_id,
            source_id=str(c["source_id"]),
            title=str(c.get("title", "")),
            source_type=str(c.get("type", "doc")),
            text=str(c["text"]),
            heading_path="",
        )
        for chunk_id, c in spec.items()
    }


def proposal_of(spec: dict[str, Any]) -> DraftProposal:
    """数据集里的草稿 → DraftProposal(与模型输出走同一套 schema 校验)。"""
    return DraftProposal.model_validate(spec)


class FixtureKbReader:
    """只认夹具里那几块的知识库读取。检索不看问题,按夹具顺序返回全部块。"""

    def __init__(self, chunks: dict[str, ChunkRecord]) -> None:
        self._chunks = chunks

    async def search(self, query: str) -> list[SearchHit]:
        return [
            SearchHit(
                source_id=c.source_id,
                title=c.title,
                chunk_text=c.text,
                heading_path="",
                score=0.5,
                chunk_id=c.chunk_id,
                source_type=c.source_type,
            )
            for c in self._chunks.values()
        ]

    async def read_source(self, source_id: str) -> list[ChunkRecord]:
        return [c for c in self._chunks.values() if c.source_id == source_id]

    async def get_chunks(self, chunk_ids: Sequence[str]) -> dict[str, ChunkRecord]:
        return {i: self._chunks[i] for i in chunk_ids if i in self._chunks}
