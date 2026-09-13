"""kb_probes executor 单测(#148):阈值/负例/分布,search 注入 fake,不碰 PG。"""

import math
from dataclasses import dataclass
from pathlib import Path

import pytest

from official_agent.evals import kb_probes


@dataclass(frozen=True)
class _Hit:
    title: str
    score: float


def _dataset(tmp_path: Path, *, min_recall: float = 0.95, neg_max: float = 0.55) -> Path:
    content = f"""
defaults:
  top_k: 3
  negative_max_score: {neg_max}
  min_recall_at_3: {min_recall}
  min_mrr: 0.75
probes:
  - id: pos_a
    kind: positive
    query: 部门?
    expect: {{ any_of_sources: ["部门介绍"] }}
  - id: pos_b
    kind: positive
    query: 招新?
    expect: {{ any_of_sources: ["招新流程 FAQ"] }}
  - id: neg_x
    kind: negative
    query: 天气?
"""
    path = tmp_path / "kb_probes.yaml"
    path.write_text(content, encoding="utf-8")
    return path


async def test_all_hit_and_negative_quiet_passes(tmp_path: Path) -> None:
    async def search(query: str, top_k: int) -> list[_Hit]:
        if query == "天气?":  # 负例:知识库不含,低分
            return [_Hit("社团介绍", 0.21)]
        return [_Hit("部门介绍", 0.82), _Hit("招新流程 FAQ", 0.70), _Hit("社团介绍", 0.40)]

    result = await kb_probes.run_suite(_dataset(tmp_path), search=search)
    assert result.status == "PASS"
    assert result.metrics["recall_at_3"] == pytest.approx(1.0)
    # pos_a 命中 rank1、pos_b 命中 rank2 → MRR = (1 + 0.5) / 2
    assert result.metrics["mrr"] == pytest.approx(0.75)
    assert len(result.cases) == 3


async def test_miss_and_false_hit_fail_with_details(tmp_path: Path) -> None:
    async def search(query: str, top_k: int) -> list[_Hit]:
        if "招新" in query:
            return [_Hit("社团介绍", 0.70), _Hit("部门介绍", 0.60)]  # 漏招新 FAQ → MISS
        if query == "天气?":
            return [_Hit("社团介绍", 0.10)]
        return [_Hit("部门介绍", 0.82)]

    async def search_with_false_hit(query: str, top_k: int) -> list[_Hit]:
        if query == "天气?":
            return [_Hit("招新流程 FAQ", 0.90)]  # 负例 0.90 ≥ 0.55 → 误命中
        if "招新" in query:
            return [_Hit("社团介绍", 0.70), _Hit("部门介绍", 0.60)]
        return [_Hit("部门介绍", 0.82), _Hit("招新流程 FAQ", 0.70)]

    result = await kb_probes.run_suite(_dataset(tmp_path), search=search)
    assert result.status == "FAIL"
    failed = {c.id for c in result.cases if not c.passed}
    assert failed == {"pos_b"}
    # recall 1/2 = 0.5 < 0.95 基线;MRR = (1+0)/2 = 0.5 < 0.75,双重门槛爆
    assert result.metrics["recall_at_3"] == pytest.approx(0.5)
    assert math.isclose(result.metrics["mrr"], 0.5)

    result2 = await kb_probes.run_suite(_dataset(tmp_path), search=search_with_false_hit)
    assert result2.status == "FAIL"
    # 该 fake 里 pos_b 仍走漏检分支 → 两个都炸
    assert {c.id for c in result2.cases if not c.passed} == {"pos_b", "neg_x"}


async def test_threshold_breach_fails_even_if_all_probes_pass(tmp_path: Path) -> None:
    """全部命中但都排 rank2 → recall 达标、MRR 低于基线,门禁仍 FAIL(OBS-07)。"""

    async def search(query: str, top_k: int) -> list[_Hit]:
        if query == "天气?":
            return [_Hit("社团介绍", 0.21)]
        if "招新" in query:
            return [_Hit("社团介绍", 0.82), _Hit("招新流程 FAQ", 0.70)]
        return [_Hit("社团介绍", 0.82), _Hit("部门介绍", 0.70)]

    result = await kb_probes.run_suite(_dataset(tmp_path), search=search)
    assert result.status == "FAIL"
    assert result.metrics["recall_at_3"] == pytest.approx(1.0)  # 用例全过
    assert result.metrics["mrr"] == pytest.approx(0.5)  # 指标门槛单独炸


async def test_distribution_notes_present(tmp_path: Path) -> None:
    async def search(query: str, top_k: int) -> list[_Hit]:
        if query == "天气?":
            return [_Hit("社团介绍", 0.21)]
        return [_Hit("部门介绍", 0.82), _Hit("招新流程 FAQ", 0.70)]

    result = await kb_probes.run_suite(_dataset(tmp_path), search=search, distribution=True)
    assert any("golden top1 分布" in n for n in result.notes)
    assert any("negative top1 分布" in n for n in result.notes)


async def test_search_exception_propagates_as_case_failure_semantics(tmp_path: Path) -> None:
    """embedding 未配置类异常由 CLI 环境闸与引擎 SKIP 兜;executor 内炸 = FAIL case。"""

    async def search(query: str, top_k: int) -> list[_Hit]:
        if query == "部门?":
            raise RuntimeError("boom")
        return [_Hit("招新流程 FAQ", 0.70)]

    with pytest.raises(RuntimeError):
        await kb_probes.run_suite(_dataset(tmp_path), search=search)
