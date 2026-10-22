"""memory_probes 执行器单测:模型注入假实现,不调真实 LLM。"""

import json
from pathlib import Path

from official_agent.evals import memory_probes
from official_agent.evals.engine import default_registry


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class _Model:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = iter(outputs)

    async def ainvoke(self, prompt: str) -> _Reply:
        return _Reply(json.dumps(next(self.outputs), ensure_ascii=False))


async def test_suite_checks_add_empty_and_update(tmp_path: Path) -> None:
    dataset = tmp_path / "memory_probes.yaml"
    dataset.write_text(
        """
runner: memory_probes
defaults: { min_pass_rate: 1.0 }
cases:
  - id: add
    said: ["我负责技术部面试"]
    expect: { add_contains: ["技术部"] }
  - id: empty
    said: ["帮我查场次"]
    expect: { expect_empty: true }
  - id: update
    existing: [{ memory_id: mem_a, content: 负责技术部 }]
    said: ["改管项目部了"]
    expect: { update_ids: [mem_a] }
""",
        encoding="utf-8",
    )
    model = _Model(
        [
            {"add": [{"content": "负责技术部的面试", "kind": "fact"}]},
            {"add": [{"content": "要查明天的场次", "kind": "fact"}]},  # 不该记
            {"update": [{"memory_id": "mem_a", "content": "负责项目部"}]},
        ]
    )
    result = await memory_probes.run_suite(dataset, model=model)
    assert [c.passed for c in result.cases] == [True, False, True]
    assert result.status == "FAIL"


def test_registered_and_dataset_parses() -> None:
    import yaml

    assert "memory_probes" in default_registry()
    root = Path(__file__).resolve().parent.parent
    data = yaml.safe_load((root / "evals/datasets/memory_probes.yaml").read_text("utf-8"))
    assert data["runner"] == "memory_probes" and len(data["cases"]) >= 5
