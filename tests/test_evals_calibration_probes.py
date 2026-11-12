"""calibration_probes 执行器单测:确定性用例直接跑,要嵌入模型的用例换成假嵌入。"""

from pathlib import Path

import pytest

from official_agent.evals import calibration_probes
from official_agent.evals.engine import default_registry

DATASET = Path(__file__).resolve().parent.parent / "evals/datasets/calibration_probes.yaml"

#: 假嵌入用的「词表」:文本里出现哪个词,对应的维度就是 1。足够区分 Web 项目、算法竞赛、海报。
_VOCAB = ("部署", "同学在用", "竞赛", "算法", "海报", "排版")


async def _keyword_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0 if word in text else 0.0 for word in _VOCAB] for text in texts]


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "calibration_probes.yaml"
    path.write_text("runner: calibration_probes\ncases:\n" + body, encoding="utf-8")
    return path


def test_suite_is_registered_and_needs_no_environment() -> None:
    spec = default_registry()["calibration_probes"]
    assert spec.env_blocker() is None


async def test_shipped_dataset_passes_and_skips_embedding_cases_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配嵌入模型时,确定性用例照跑照判,要嵌入的用例单条跳过、不计分——不能整套变绿或整套跳过。"""
    monkeypatch.setattr(calibration_probes, "_embedding_configured", lambda: False)

    result = await calibration_probes.run_suite(DATASET)

    assert result.status == "PASS"
    assert [c.id for c in result.cases if not c.passed] == []
    skipped = [c.id for c in result.cases if c.skipped]
    assert len(skipped) == 2 and all(cid.startswith("retrieve_") for cid in skipped)
    assert len(result.cases) - len(skipped) >= 13
    assert result.pass_rate == 1.0  # 跳过的不进分母
    assert any("未跑" in note for note in result.notes)


async def test_embedding_cases_run_through_the_real_selection_with_an_injected_embedder() -> None:
    result = await calibration_probes.run_suite(DATASET, embed=_keyword_embed)

    assert result.status == "PASS"
    assert not any(c.skipped for c in result.cases)


async def test_a_seed_that_should_be_found_but_is_not_fails_the_suite(tmp_path: Path) -> None:
    dataset = _write(
        tmp_path,
        """
  - id: retrieve_miss
    kind: retrieve
    seeds:
      - checklist: 媒体部
        item: 相关经历作品
        met: true
        quote: "做过十几张活动海报"
        reason: "有作品"
    resume: ["参加过算法竞赛"]
    expect: { retrieved: [0] }
""",
    )

    result = await calibration_probes.run_suite(dataset, embed=_keyword_embed)

    assert result.status == "FAIL"
    assert result.cases[0].detail == "第 0 条种子判例没被检到"


async def test_each_kind_reports_what_went_wrong(tmp_path: Path) -> None:
    dataset = _write(
        tmp_path,
        """
  - id: screen_should_reject
    kind: screen
    case: { checklist: 技术部, item: 技术基础, met: true, quote: "写了小程序", reason: "有产出" }
    expect: { rejected: true }
  - id: screen_should_mask
    kind: screen
    case: { checklist: 技术部, item: 技术基础, met: true, quote: "写了小程序", reason: "有产出" }
    expect: { rejected: false, lacks: ["小程序"] }
  - id: eligible_wrong
    kind: eligible
    cases: [{ case_id: 1 }, { case_id: 5 }]
    scoring: { watermark: 3, cycle_id: 9, resume_id: 1 }
    expect: { usable: [1, 5] }
  - id: select_wrong
    kind: select
    cases:
      - case_id: 1
        checklist: 技术部
        item: 技术基础
        met: true
        quote: q
        reason: r
        vector: [0.0, 1.0]
    fragments: [[1.0, 0.0]]
    expect: { selected: [1] }
""",
    )

    result = await calibration_probes.run_suite(dataset)

    details = {c.id: c.detail for c in result.cases}
    assert result.status == "FAIL"
    assert details["screen_should_reject"] == "该被拒的判例通过了筛查"
    assert details["screen_should_mask"] == "入库文本里还有「小程序」"
    assert details["eligible_wrong"] == "可用案例=[1] 期望 [1, 5]"
    assert details["select_wrong"] == "选中的案例=[] 期望 [1]"


async def test_unknown_kind_is_a_dataset_error_not_a_failed_case(tmp_path: Path) -> None:
    dataset = _write(tmp_path, "  - { id: typo, kind: selct }\n")

    with pytest.raises(ValueError, match="未知的 kind 'selct'"):
        await calibration_probes.run_suite(dataset)
