"""草稿查重:新草稿的问题与已有问法是不是同一个问题。纯函数。

「同一个问题」沿用聚类的判据——问题向量的余弦相似度达到聚类阈值。向量由调用方
现算后传入。查重只决定草稿的去向(转成补问法、顶替旧草稿、丢弃),内容冲突不在
这里裁决,只标出来交给人。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from official_agent.kb_ops.cluster import unit_vector


@dataclass(frozen=True)
class KnownQuestion:
    """一个已有的问法。ref 是它的归属:FAQ 的 source_id,或待审草稿的 draft_id。"""

    ref: str
    text: str
    vector: Sequence[float]


def most_similar(
    vector: Sequence[float], known: Sequence[KnownQuestion], threshold: float
) -> KnownQuestion | None:
    """known 里与 vector 最相似且达到阈值的那个问法;都不到阈值返回 None。

    相似度相同时取排在前面的。
    """
    target = unit_vector(vector)
    best: KnownQuestion | None = None
    best_similarity = -1.0
    for candidate in known:
        similarity = math.sumprod(target, unit_vector(candidate.vector))
        if similarity >= threshold and similarity > best_similarity:
            best, best_similarity = candidate, similarity
    return best


def similar_refs(
    vector: Sequence[float], known: Sequence[KnownQuestion], threshold: float
) -> list[str]:
    """known 里所有与 vector 达到阈值的问法的 ref,去重,保持 known 的顺序。"""
    target = unit_vector(vector)
    refs: list[str] = []
    for candidate in known:
        similarity = math.sumprod(target, unit_vector(candidate.vector))
        if similarity >= threshold and candidate.ref not in refs:
            refs.append(candidate.ref)
    return refs
