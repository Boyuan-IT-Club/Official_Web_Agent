"""起草与审核读知识库的实现:走 kb/store,口径与在线检索一致。

与在线的 search_knowledge 相比只有一处不同:检索不设门槛(起草要读到「差一点」的
条目才能学到官方用词)。周期过滤照旧——往届的面试时间地点不该被起草成新条目。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from official_agent.kb import store as kb_store
from official_agent.kb.embedding import Embedder
from official_agent.kb.store import ChunkRecord, SearchHit

#: 起草检索每次返回的块数
_SEARCH_TOP_K = 5


class StoreKbReader:
    """kb_ops.draft.KbReader 的生产实现。

    cycle_ids 是当前开放的招新周期(空 = 只读常青)。embedder 缺省走配置的嵌入端点。
    """

    def __init__(self, cycle_ids: Sequence[int], *, embedder: Embedder | None = None) -> None:
        self._cycle_ids = tuple(cycle_ids)
        self._embedder = embedder

    async def search(self, query: str) -> list[SearchHit]:
        return await kb_store.search(
            query,
            top_k=_SEARCH_TOP_K,
            min_score=0.0,
            cycle_ids=self._cycle_ids,
            embedder=self._embedder,
        )

    async def read_source(self, source_id: str) -> list[ChunkRecord]:
        return await asyncio.to_thread(
            kb_store.list_source_chunks, source_id, cycle_ids=self._cycle_ids
        )

    async def get_chunks(self, chunk_ids: Sequence[str]) -> dict[str, ChunkRecord]:
        return await asyncio.to_thread(
            kb_store.get_chunks, list(chunk_ids), cycle_ids=self._cycle_ids
        )
