"""题目相似度标注:本包跨线查重 + 跨简历通用题检测。只标注,不删题。

两步夹在题库落库的前后(调用方是 runner 的题库段):

1. annotate —— 落库前。给每道可挑的题算向量:本包里别的证据线已经有一道
   近似的题时,证据弱的那道记 duplicate_of;本周期至少 N 份其他简历出过近似的题
   时,记 generic_resumes。结果写进信封顶层的 similarity,题目对象不动。
2. persist —— 落库后。题库版本定下来,稳定题引用 id 才有,这时把同一批向量
   落进索引(state/qbank_index.py)。

两步都 fail-open、都不抛:embedding 没配置、库里没有 pgvector、任何一步出错,
都只是这份题库没有相似度标注,题库照常落库。
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

from official_agent.config import get_settings
from official_agent.kb.embedding import EmbeddingNotConfiguredError, embed_texts
from official_agent.security.pii import mask_pii
from official_agent.state import qbank as qbank_store
from official_agent.state import qbank_index

#: 证据强弱的次序(越靠前越强):两道题问同一件事时留强的那道。仓线有代码可
#: 对证,评测线有失败用例可对证,简历线只有自述,奖项线与兜底线是模板题。
_EVIDENCE_ORDER = ("repo", "autograding", "cv_dive", "awards", "base_and_skills")


@dataclass(frozen=True)
class PendingIndex:
    """annotate 算好、等题库版本定下来再落库的那批向量。

    texts / vectors 与落库前的可挑题视图逐条对应、同序。
    """

    embed_model: str
    texts: list[str]
    vectors: list[list[float]]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else list(vector)


def _evidence_rank(entry: dict[str, Any]) -> tuple[int, int]:
    kind = str(entry.get("group_kind", ""))
    rank = _EVIDENCE_ORDER.index(kind) if kind in _EVIDENCE_ORDER else len(_EVIDENCE_ORDER)
    return rank, int(entry.get("group_index", 0))


def find_cross_line_duplicates(
    entries: list[dict[str, Any]], vectors: list[list[float]], *, min_similarity: float
) -> dict[int, int]:
    """本包里跨证据线的近似重复:返回 {弱题下标: 强题下标}。

    只比**不同组**的题——同一组里的重复归审题者管。按证据从强到弱过一遍:
    一道题若与已经留下的、别的组的题近似,就记成其中最像的那道的重复,自己不再
    作为别人的比较对象;所以强题下标指向的永远是一道被留下的题。
    入口题不会被记成重复:每条线的开场题问的都是「讲讲这个项目」,措辞相近是
    正常的。它仍可以是别的题的强题。
    """
    units = [_unit(vector) for vector in vectors]
    by_strength = sorted(range(len(entries)), key=lambda i: (*_evidence_rank(entries[i]), i))
    kept: list[int] = []
    duplicates: dict[int, int] = {}
    for index in by_strength:
        entry = entries[index]
        closest: tuple[float, int] | None = None
        if entry.get("role") != "entry":
            in_other_groups = (
                (sum(a * b for a, b in zip(units[index], units[other], strict=True)), other)
                for other in kept
                if entries[other].get("group_index") != entry.get("group_index")
            )
            # 相似度并列时取先留下的那道(证据更强)
            closest = max(in_other_groups, key=lambda pair: (pair[0], -pair[1]), default=None)
        if closest is not None and closest[0] >= min_similarity:
            duplicates[index] = closest[1]
        else:
            kept.append(index)
    return duplicates


def _locator(entry: dict[str, Any]) -> dict[str, Any]:
    return {"group_index": int(entry["group_index"]), "key": qbank_store.entry_key(entry)}


async def _generic_counts(
    vectors: list[list[float]], *, resume_id: int, cycle_id: int, embed_model: str
) -> list[int]:
    """各题在本周期其他简历里的近似份数;索引不可用时全记 0(只损失通用题标注)。"""
    settings = get_settings()
    try:
        return await asyncio.to_thread(
            qbank_index.count_similar_resumes,
            resume_id=resume_id,
            cycle_id=cycle_id,
            embed_model=embed_model,
            embeddings=vectors,
            min_similarity=settings.qbank_generic_min_similarity,
        )
    except Exception:  # noqa: BLE001 — 索引是旁路,查不了就不标通用题
        logging.getLogger(__name__).warning(
            "题目索引查询失败,跳过通用题标注 resume_id=%s", resume_id, exc_info=True
        )
        return [0] * len(vectors)


async def annotate(
    envelope: dict[str, Any], *, resume_id: int, cycle_id: int
) -> PendingIndex | None:
    """给信封加相似度标注(就地写 envelope["similarity"]),返回待落库的向量。

    返回 None = 没有做标注(开关关着、没有题、embedding 不可用),信封原样不动。
    题文先脱敏再送去算向量。
    """
    settings = get_settings()
    if not settings.qbank_index_enabled:
        return None
    entries = qbank_store.flatten_v2_pickable(
        envelope, resume_id=resume_id, cycle_id=cycle_id, qbank_version=0
    )
    if not entries:
        return None
    texts = [mask_pii(str(entry.get("question", ""))) for entry in entries]
    try:
        vectors = await embed_texts(texts)
        duplicates = await asyncio.to_thread(
            find_cross_line_duplicates,
            entries,
            vectors,
            min_similarity=settings.qbank_duplicate_min_similarity,
        )
    except EmbeddingNotConfiguredError:
        logging.getLogger(__name__).info("embedding 未配置,跳过题目相似度标注")
        return None
    except Exception:  # noqa: BLE001 — 标注是旁路,算不了就不标
        logging.getLogger(__name__).warning(
            "题目向量计算失败,跳过相似度标注 resume_id=%s", resume_id, exc_info=True
        )
        return None

    embed_model = settings.embed_model
    generic_counts = await _generic_counts(
        vectors, resume_id=resume_id, cycle_id=cycle_id, embed_model=embed_model
    )
    items: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        # 入口题不记通用:开场题本来就可以通用
        is_generic = (
            entry.get("role") != "entry"
            and generic_counts[index] >= settings.qbank_generic_min_resumes
        )
        if index not in duplicates and not is_generic:
            continue
        items.append(
            {
                **_locator(entry),
                "duplicate_of": (
                    _locator(entries[duplicates[index]]) if index in duplicates else None
                ),
                "generic_resumes": generic_counts[index] if is_generic else 0,
            }
        )
    envelope["similarity"] = {"embed_model": embed_model, "items": items}
    return PendingIndex(embed_model=embed_model, texts=texts, vectors=vectors)


async def persist(
    pending: PendingIndex | None,
    envelope: dict[str, Any],
    *,
    resume_id: int,
    cycle_id: int,
    qbank_version: int,
) -> None:
    """把 annotate 算好的向量落进索引;pending 为 None 时什么也不做。不抛。"""
    if pending is None:
        return
    entries = qbank_store.flatten_v2_pickable(
        envelope, resume_id=resume_id, cycle_id=cycle_id, qbank_version=qbank_version
    )
    if len(entries) != len(pending.vectors):
        # 两次展平之间信封不该变;对不上就宁可不落,也不把向量挂到别的题上
        logging.getLogger(__name__).warning(
            "题目索引未落库:题数与向量数不一致 resume_id=%s", resume_id
        )
        return
    rows = [
        qbank_index.IndexRow(
            ref_id=str(entry["ref_id"]),
            group_kind=str(entry.get("group_kind", "")),
            role=str(entry.get("role", "")),
            category=str(entry.get("category", "")),
            question=text,
            embedding=vector,
        )
        for entry, text, vector in zip(entries, pending.texts, pending.vectors, strict=True)
    ]
    try:
        await asyncio.to_thread(
            qbank_index.replace_resume_rows,
            resume_id=resume_id,
            cycle_id=cycle_id,
            qbank_version=qbank_version,
            embed_model=pending.embed_model,
            rows=rows,
        )
    except Exception:  # noqa: BLE001 — 索引是旁路,落不了不影响已经落库的题库
        logging.getLogger(__name__).warning(
            "题目索引落库失败 resume_id=%s", resume_id, exc_info=True
        )
