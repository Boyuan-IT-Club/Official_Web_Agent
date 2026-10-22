"""官网写操作确认:confirm 事件 → /chat/confirm 续跑;不确认直接发新消息 = 取消。"""

from __future__ import annotations

import contextlib
import json
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command, Interrupt

from official_agent.web.app import create_app

_PENDING = {
    "summary": "把简历 #9 分配到面试场次 #3",
    "confirm": True,
    "tool": "assign_interview",
    "fingerprint": "fp-123",
}


class _ConfirmingAgent:
    """首轮停在写操作确认处;记录续跑输入与取消写入。"""

    def __init__(self) -> None:
        self.pending: dict | None = None
        self.inputs: list[Any] = []
        self.cancel_updates: list[dict] = []

    async def astream(self, graph_input, *, config, **kwargs):
        self.inputs.append(graph_input)
        if isinstance(graph_input, Command):
            self.pending = None
            yield "messages", (AIMessageChunk(content="已完成分配"), {})
            return
        if len(self.inputs) == 1:
            self.pending = dict(_PENDING)
            yield "updates", {"__interrupt__": (Interrupt(value=dict(_PENDING)),)}
            return
        yield "messages", (AIMessageChunk(content="新问题的回答"), {})

    async def aget_state(self, config):
        interrupts = (Interrupt(value=self.pending),) if self.pending else ()
        last = AIMessage(
            "", tool_calls=[{"name": "assign_interview", "args": {}, "id": "c1"}]
        )
        messages = [HumanMessage("排一下"), last] if self.pending else []
        return types.SimpleNamespace(interrupts=interrupts, values={"messages": messages})

    async def aupdate_state(self, config, values, as_node=None):
        self.cancel_updates.append({"values": values, "as_node": as_node})
        self.pending = None


@contextlib.asynccontextmanager
async def _live_checkpointer() -> AsyncIterator[object]:
    yield object()  # 真值:写工具与确认需要 checkpointer


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, web_no_real_pg: None):
    from official_agent.web import routes

    agent = _ConfirmingAgent()
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _live_checkpointer)

    async def _resolve(*_a, **_k):
        return {
            "user_id": 8,
            "role": "admin",
            "role_names": ["管理员"],
            "permission_codes": ["interview:schedule"],
            "source": "web",
        }

    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: agent)
    monkeypatch.setattr(
        "official_agent.web.routes.create_thread",
        lambda *a, **k: types.SimpleNamespace(thread_id="web:u8:abcdefgh"),
    )
    monkeypatch.setattr(
        "official_agent.web.agent_factory.config_fingerprint", lambda: "same"
    )
    monkeypatch.setattr("official_agent.web.telemetry.log_conversation", lambda *a, **k: None)
    with TestClient(create_app()) as client:
        yield client, agent


_AUTH = {"Authorization": "Bearer admin-jwt"}


def _events(resp) -> list[dict]:
    body = "".join(resp.iter_text())
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data: ")]


def _start(client) -> tuple[str, list[dict]]:
    events = _events(client.post("/api/agent/chat", json={"message": "排一下"}, headers=_AUTH))
    session_id = next(e["session_id"] for e in events if e["type"] == "session")
    return session_id, events


def test_interrupt_becomes_confirm_event(setup) -> None:
    client, _ = setup
    _, events = _start(client)
    confirm = next(e for e in events if e["type"] == "confirm")
    assert confirm["summary"] == _PENDING["summary"]
    assert confirm["fingerprint"] == "fp-123" and confirm["tool"] == "assign_interview"
    assert events[-1]["type"] == "done"


def test_confirm_resumes_with_decision_and_fingerprint(setup) -> None:
    client, agent = setup
    session_id, _ = _start(client)
    resp = client.post(
        "/api/agent/chat/confirm",
        json={"session_id": session_id, "decision": "approve", "fingerprint": "fp-123"},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    events = _events(resp)
    assert any(e.get("content") == "已完成分配" for e in events if e["type"] == "delta")
    resumed = agent.inputs[-1]
    assert isinstance(resumed, Command)
    assert resumed.resume == {"decision": "approve", "fingerprint": "fp-123"}


def test_confirm_rejects_stale_fingerprint_and_missing_pending(setup) -> None:
    client, _ = setup
    session_id, _ = _start(client)
    stale = client.post(
        "/api/agent/chat/confirm",
        json={"session_id": session_id, "decision": "approve", "fingerprint": "old"},
        headers=_AUTH,
    )
    assert stale.status_code == 409 and "过期" in stale.json()["detail"]
    # 拒绝不需要指纹
    ok = client.post(
        "/api/agent/chat/confirm",
        json={"session_id": session_id, "decision": "reject"},
        headers=_AUTH,
    )
    assert ok.status_code == 200
    _events(ok)
    again = client.post(
        "/api/agent/chat/confirm",
        json={"session_id": session_id, "decision": "reject"},
        headers=_AUTH,
    )
    assert again.status_code == 409


def test_new_message_cancels_pending_operation(setup) -> None:
    client, agent = setup
    session_id, _ = _start(client)
    events = _events(
        client.post(
            "/api/agent/chat",
            json={"message": "先问个别的", "session_id": session_id},
            headers=_AUTH,
        )
    )
    assert any(e.get("content") == "新问题的回答" for e in events if e["type"] == "delta")
    (update,) = agent.cancel_updates
    assert update["as_node"] == "tools"
    (tool_msg,) = update["values"]["messages"]
    assert tool_msg.tool_call_id == "c1" and "已取消" in tool_msg.content
