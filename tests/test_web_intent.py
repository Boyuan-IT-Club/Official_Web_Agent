"""官网聊天的逐轮意图提示:三档开关、记录落账、确认续跑不判断、判断失败不拖垮对话。"""

from __future__ import annotations

import contextlib
import json
import logging
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.types import Command, Interrupt

from official_agent.security.injection_guard import guard_tool_result
from official_agent.web.app import create_app

_AUTH = {"Authorization": "Bearer admin-jwt"}
_PENDING = {"summary": "把简历 #11 分配到面试场次 #6", "confirm": True,
            "tool": "assign_interview", "fingerprint": "fp-1"}


def _listing() -> list[Any]:
    data = [{"sessionId": 5 + i, "date": "2026-09-11", "start": f"1{4 + i}:00"} for i in range(3)]
    wrapped, _ = guard_tool_result("find_available_sessions", {"status": "ok", "data": data})
    return [
        HumanMessage("9 月 11 日有哪些场次"),
        ToolMessage(wrapped, tool_call_id="c0", name="find_available_sessions"),
        AIMessage("有三个场次"),
    ]


class _RecordingAgent:
    """记录每轮的输入、config 与运行时上下文;state 可预置消息与挂起的确认。"""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.messages: list[Any] = []
        self.pending: dict | None = None

    async def astream(self, graph_input, *, config, context=None, **kwargs):
        self.runs.append({"input": graph_input, "config": config, "context": context or {}})
        if isinstance(graph_input, Command):
            self.pending = None
        yield "messages", (AIMessageChunk(content="好的"), {})

    async def aget_state(self, config):
        interrupts = (Interrupt(value=self.pending),) if self.pending else ()
        messages = list(self.messages)
        if self.pending:
            call = {"name": "assign_interview", "args": {}, "id": "w1"}
            messages.append(AIMessage("", tool_calls=[call]))
        return types.SimpleNamespace(interrupts=interrupts, values={"messages": messages})

    async def aupdate_state(self, config, values, as_node=None):
        self.pending = None


@contextlib.asynccontextmanager
async def _live_checkpointer() -> AsyncIterator[object]:
    yield object()


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch, web_no_real_pg: None):
    from official_agent.config import get_settings
    from official_agent.web import routes

    logged: list[dict[str, Any]] = []
    agent = _RecordingAgent()

    async def _resolve(*_a, **_k):
        return {"user_id": 8, "role": "admin", "role_names": ["管理员"],
                "permission_codes": ["interview:schedule"], "source": "web"}

    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _live_checkpointer)
    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: agent)
    monkeypatch.setattr(
        "official_agent.web.routes.create_thread",
        lambda *a, **k: types.SimpleNamespace(thread_id="web:u8:intent00"),
    )
    monkeypatch.setattr("official_agent.web.agent_factory.config_fingerprint", lambda: "same")
    monkeypatch.setattr(
        "official_agent.web.telemetry.log_conversation", lambda *a, **k: logged.append(k)
    )

    @contextlib.contextmanager
    def _make(mode: str):
        settings = get_settings().model_copy(update={"assistant_intent_mode": mode})
        monkeypatch.setattr("official_agent.config.get_effective_settings", lambda: settings)
        with TestClient(create_app()) as client:
            yield client, agent, logged

    return _make


def _events(resp) -> list[dict]:
    body = "".join(resp.iter_text())
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data: ")]


def _chat(client, message: str, session_id: str | None = None) -> list[dict]:
    body: dict[str, Any] = {"message": message}
    if session_id:
        body["session_id"] = session_id
    return _events(client.post("/api/agent/chat", json=body, headers=_AUTH))


def test_off_mode_computes_nothing(make_client) -> None:
    with make_client("off") as (client, agent, logged):
        events = _chat(client, "周六下午还有空位吗?")
    assert events[-1]["type"] == "done"
    assert agent.runs[0]["context"]["intent_hint"] == ""
    assert "intent_label" not in agent.runs[0]["config"]["metadata"]
    assert logged[0]["intent"] is None


def test_shadow_mode_records_but_does_not_hint(make_client) -> None:
    with make_client("shadow") as (client, agent, logged):
        _chat(client, "周六下午还有空位吗?")
    assert agent.runs[0]["context"]["intent_hint"] == ""
    record = logged[0]["intent"]
    assert record["mode"] == "shadow" and record["label"] == "admin_lookup"
    assert record["hinted"] is False and record["hint_prompt"] is None
    assert "find_available_sessions" in record["tools"]
    assert isinstance(record["elapsed_ms"], int)
    assert agent.runs[0]["config"]["metadata"]["intent_label"] == "admin_lookup"


def test_hint_mode_passes_hint_built_from_session_state(make_client) -> None:
    with make_client("hint") as (client, agent, logged):
        agent.messages = _listing()
        _chat(client, "那就第二个")
    hint = agent.runs[0]["context"]["intent_hint"]
    assert "# 本轮意图提示" in hint and "场次 #6" in hint
    record = logged[0]["intent"]
    assert record["label"] == "schedule_interview" and record["chosen_option"] == 6
    assert record["hinted"] is True and record["hint_prompt"]
    assert "prior_options" in record["evidence"]


def test_plain_ok_message_cancels_pending_and_is_not_a_confirmation(make_client) -> None:
    """聊天里发「好的」是普通消息:挂起的操作被取消,不走 Command 续跑。"""
    with make_client("hint") as (client, agent, logged):
        agent.pending = dict(_PENDING)
        _chat(client, "好的")
    run = agent.runs[0]
    assert not isinstance(run["input"], Command)
    assert agent.pending is None
    assert "pending_cancelled" in logged[0]["intent"]["evidence"]


def test_confirm_resume_skips_intent(make_client) -> None:
    with make_client("hint") as (client, agent, logged):
        session_id = next(
            e["session_id"] for e in _chat(client, "排一下") if e["type"] == "session"
        )
        agent.pending = dict(_PENDING)
        resp = client.post(
            "/api/agent/chat/confirm",
            json={"session_id": session_id, "decision": "approve", "fingerprint": "fp-1"},
            headers=_AUTH,
        )
        _events(resp)
    resumed = agent.runs[-1]
    assert isinstance(resumed["input"], Command)
    assert resumed["context"]["intent_hint"] == ""
    assert logged[-1]["intent"] is None


def test_classifier_failure_is_fail_open(make_client, monkeypatch, caplog) -> None:
    from official_agent.graphs.assistant import intent as ti

    def _boom(*_a, **_k):
        raise RuntimeError("rules exploded")

    monkeypatch.setattr(ti, "classify_turn", _boom)
    with make_client("hint") as (client, agent, logged), caplog.at_level(logging.WARNING):
        events = _chat(client, "周六下午还有空位吗?")
    assert events[-1]["type"] == "done"
    assert agent.runs[0]["context"]["intent_hint"] == ""
    assert logged[0]["intent"] is None
    assert any("意图判断失败" in r.message for r in caplog.records)
