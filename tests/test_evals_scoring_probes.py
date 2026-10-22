"""scoring_probes 执行器单测:评分入口注入假实现,不调模型。"""

from pathlib import Path

from official_agent.evals import scoring_probes
from official_agent.evals.engine import default_registry


def _card(**over) -> dict:
    base = {
        "hard_zero": False,
        "match_score": 8.0,
        "effort_score": 7.0,
        "transfer_hint": None,
        "interview_hints": [],
    }
    base.update(over)
    return base


def test_check_card_ranges_transfer_and_hints() -> None:
    assert scoring_probes.check_card(_card(), {"match_min": 7, "effort_max": 8}) == []
    problems = scoring_probes.check_card(
        _card(match_score=2.0, transfer_hint={"dept": "项目部"}, interview_hints=["a"]),
        {"match_min": 7, "transfer": "媒体部", "hints_contain": ["非计算机类"]},
    )
    assert len(problems) == 3


def test_check_card_null_transfer_means_none_expected() -> None:
    assert scoring_probes.check_card(_card(), {"transfer": None}) == []
    assert scoring_probes.check_card(
        _card(transfer_hint={"dept": "媒体部"}), {"transfer": None}
    )


def test_check_card_missing_score_is_reported() -> None:
    assert scoring_probes.check_card(_card(match_score=None), {"match_min": 1}) == ["match 缺失"]


async def test_run_suite_gates_on_pass_rate(tmp_path: Path) -> None:
    dataset = tmp_path / "scoring_probes.yaml"
    dataset.write_text(
        """
runner: scoring_probes
defaults: { min_pass_rate: 0.6 }
cases:
  - id: ok
    profile: { first_dept: 技术部 }
    fields: [{ title: 简介, value: 写过项目 }]
    expect: { match_min: 7 }
  - id: bad
    profile: { first_dept: 技术部 }
    fields: [{ title: 简介, value: 写过项目 }]
    expect: { match_min: 9 }
""",
        encoding="utf-8",
    )
    seen: list = []

    async def fake_score(fields, *, resume_id, cycle_id, profile):
        seen.append((fields[0]["title"], profile.first_dept))
        return _card()

    result = await scoring_probes.run_suite(dataset, score=fake_score)
    assert result.status == "FAIL"  # 1/2 = 0.5 < 0.6
    assert result.metrics["scoring_pass_rate"] == 0.5
    assert seen == [("简介", "技术部"), ("简介", "技术部")]


def test_registered_and_dataset_parses() -> None:
    import yaml

    assert "scoring_probes" in default_registry()
    root = Path(__file__).resolve().parent.parent
    data = yaml.safe_load((root / "evals/datasets/scoring_probes.yaml").read_text("utf-8"))
    assert data["runner"] == "scoring_probes"
    assert len(data["cases"]) >= 8
