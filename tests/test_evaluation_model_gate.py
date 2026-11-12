"""评估域模型并发闸单测:上限、结构化调用同走一把闸、探索的墙钟不含排队时间。"""

import asyncio
import types
import weakref
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from official_agent.config import get_settings
from official_agent.evaluation import explore as explore_mod
from official_agent.evaluation import llm_common
from official_agent.evaluation.explore import explore_repo


@pytest.fixture
def gate_limit(monkeypatch: pytest.MonkeyPatch):
    """设定闸的上限并换一把新闸:闸按事件循环缓存,不重置就可能沿用别的用例建的那把。"""

    def _set(limit: int) -> None:
        monkeypatch.setattr(get_settings(), "evaluation_model_concurrency", limit)
        monkeypatch.setattr(llm_common, "_model_gates", weakref.WeakKeyDictionary())

    return _set


class _CountingModel:
    """记下同时在途的调用数峰值;每次调用让出几次事件循环。"""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    async def ainvoke(self, messages: list) -> AIMessage:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            return AIMessage(content="{}")
        finally:
            self.in_flight -= 1


async def test_gate_caps_concurrent_model_calls(gate_limit) -> None:
    gate_limit(2)
    model = _CountingModel()

    await asyncio.gather(*(llm_common.gated_ainvoke(model, []) for _ in range(7)))

    assert model.peak == 2


async def test_structured_calls_go_through_the_same_gate(gate_limit) -> None:
    """评分、出题、审题都经 invoke_with_retry 调模型:它与直接过闸的调用共用一把闸。"""
    gate_limit(2)
    model = _CountingModel()
    structured = (
        llm_common.invoke_with_retry(
            model, "prompt", parse=lambda content: content, build_corrective=str, fail_message="x"
        )
        for _ in range(3)
    )
    direct = (llm_common.gated_ainvoke(model, []) for _ in range(3))

    await asyncio.gather(*structured, *direct)

    assert model.peak == 2


async def test_nonpositive_limit_still_lets_one_call_through(gate_limit) -> None:
    """上限配成 0 不该把评估域整个堵死。"""
    gate_limit(0)
    model = _CountingModel()

    await asyncio.wait_for(
        asyncio.gather(*(llm_common.gated_ainvoke(model, []) for _ in range(3))), timeout=5
    )

    assert model.peak == 1


class _ScriptedModel:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_ScriptedModel":
        return self

    async def ainvoke(self, messages: list) -> Any:
        return self._responses.pop(0)


class _RepoMetaOnlyClient:
    def __init__(self) -> None:
        self.calls = 0

    async def repo_meta(self, owner: str, repo: str) -> dict:
        self.calls += 1
        return {"full_name": f"{owner}/{repo}", "fork": False}


async def test_explore_wall_clock_excludes_time_queued_at_the_gate(
    gate_limit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """排队等闸的时间不算进探索预算:别的 job 占着闸再久,这条线的档案也不该变薄。

    用假时钟:探索在闸前排队期间,时钟被拨快到远超墙钟上限。排队若计入预算,
    随后的工具调用会被判墙钟触顶、档案标降级。
    """
    gate_limit(1)
    clock = types.SimpleNamespace(now=0.0)
    monkeypatch.setattr(explore_mod, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    model = _ScriptedModel(
        [
            AIMessage("", tool_calls=[{"name": "repo_meta", "args": {}, "id": "c0"}]),
            AIMessage(content="材料够了"),
        ]
    )
    client = _RepoMetaOnlyClient()

    gate = llm_common.model_gate()
    await gate.acquire()  # 别的评估任务占着闸
    exploring = asyncio.create_task(
        explore_repo(
            project_text="我做了 xx",
            owner="me",
            name="demo",
            attribution="trusted-own",
            login="me",
            client=client,  # type: ignore[arg-type]
            model=model,  # type: ignore[arg-type]
        )
    )
    for _ in range(5):  # 让探索走到闸前排队
        await asyncio.sleep(0)
    clock.now += explore_mod.MAX_WALL_SECONDS * 10
    gate.release()
    dossier = await asyncio.wait_for(exploring, timeout=5)

    assert client.calls == 1  # 工具照常执行
    assert not dossier.degraded
