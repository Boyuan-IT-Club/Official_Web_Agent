"""对话写工具:确认 → 意图审计 → 本人身份执行 → 结果审计,全流程走真实 LangGraph 图。"""

from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from official_agent.graphs.assistant import assemble_tools, tool_contract, tool_roster
from official_agent.tools import readonly, write
from official_agent.tools.client import BackendError, BackendUnavailableError
from official_agent.tools.envelope import mount_error_envelope
from official_agent.tools.interrupt_guard import operation_fingerprint


class _Model(BaseChatModel):
    """先调一次写工具,拿到工具结果后回一句话。"""

    plan: Any = None

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_Model":  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=next(self.plan))])


def _agent(tool_name: str, args: dict) -> Any:
    plan = iter(
        [
            AIMessage("", tool_calls=[{"name": tool_name, "args": args, "id": "c1"}]),
            AIMessage("好的"),
        ]
    )
    tool = mount_error_envelope(write.WRITE_TOOLS[tool_name])
    return create_agent(_Model(plan=plan), tools=[tool], checkpointer=MemorySaver())


class _Backend:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.error = error

    async def request_as_user(self, method, path, *, json=None, user_token="", params=None):
        self.calls.append((method, path, json, user_token))
        if self.error:
            raise self.error
        return {"ok": True}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    backend = _Backend()
    audits: list[dict] = []

    async def _client():
        return backend

    monkeypatch.setattr(readonly, "get_backend_client", _client)
    monkeypatch.setattr("official_agent.state.audit.write_audit", lambda **k: audits.append(k))
    return backend, audits


_CFG = {"configurable": {"thread_id": "web:u8:abc"}, "metadata": {"user_id": 8}}


async def _run_until_confirm(agent: Any) -> dict:
    async with readonly.asker_scope("admin-jwt"):
        out = await agent.ainvoke({"messages": [HumanMessage("排一下")]}, _CFG)
    (pending,) = out["__interrupt__"]
    return pending.value


async def _resume(agent: Any, resume: Any) -> dict:
    async with readonly.asker_scope("admin-jwt"):
        out = await agent.ainvoke(Command(resume=resume), _CFG)
    tool_msg = next(m for m in out["messages"] if m.type == "tool")
    import json

    return json.loads(tool_msg.content)


async def test_approve_executes_as_asker_and_audits_twice(env) -> None:
    backend, audits = env
    agent = _agent("assign_interview", {"resume_id": 9, "target_session_id": 3})
    pending = await _run_until_confirm(agent)
    assert "简历 #9" in pending["summary"] and pending["tool"] == "assign_interview"
    assert pending["fingerprint"] == operation_fingerprint(
        "assign_interview", {"resume_id": 9, "target_session_id": 3}
    )
    assert backend.calls == []  # 挂起时零副作用

    result = await _resume(agent, {"decision": "approve", "fingerprint": pending["fingerprint"]})
    assert result["status"] == "done"
    assert backend.calls == [
        ("POST", "/api/interview/admin/preferences/9/assign", {"targetSessionId": 3}, "admin-jwt")
    ]
    assert len(audits) == 2  # 意图 + 结果
    assert audits[0]["acting_user_id"] == 8 and audits[0]["thread_id"] == "web:u8:abc"
    assert audits[0]["action"] == {"op": "assign_interview", "resume_id": 9, "target_session_id": 3}


async def test_wrong_fingerprint_does_not_execute(env) -> None:
    backend, audits = env
    agent = _agent("assign_interview", {"resume_id": 9, "target_session_id": 3})
    await _run_until_confirm(agent)
    result = await _resume(agent, {"decision": "approve", "fingerprint": "forged"})
    assert result["status"] == "cancelled" and "不一致" in result["message"]
    assert backend.calls == [] and audits == []


async def test_reject_does_not_execute(env) -> None:
    backend, audits = env
    agent = _agent("handle_reschedule", {"request_id": 4, "status": 1, "admin_note": ""})
    await _run_until_confirm(agent)
    result = await _resume(agent, {"decision": "reject"})
    assert result["status"] == "cancelled"
    assert backend.calls == [] and audits == []


async def test_intent_audit_failure_blocks_execution(env, monkeypatch) -> None:
    backend, _ = env

    def _boom(**k):
        raise RuntimeError("pg down")

    monkeypatch.setattr("official_agent.state.audit.write_audit", _boom)
    agent = _agent("assign_interview", {"resume_id": 9, "target_session_id": 3})
    pending = await _run_until_confirm(agent)
    result = await _resume(agent, {"decision": "approve", "fingerprint": pending["fingerprint"]})
    assert result["status"] == "error" and "审计" in result["message"]
    assert backend.calls == []


@pytest.mark.parametrize(
    ("error", "status"),
    [(BackendUnavailableError("超时"), "unknown"), (BackendError("场次已满"), "error")],
)
async def test_backend_failures_are_reported_honestly(env, error, status) -> None:
    backend, audits = env
    backend.error = error
    agent = _agent("assign_interview", {"resume_id": 9, "target_session_id": 3})
    pending = await _run_until_confirm(agent)
    result = await _resume(agent, {"decision": "approve", "fingerprint": pending["fingerprint"]})
    assert result["status"] == status
    assert len(backend.calls) == 1  # 不重试:超时时请求可能已到达后端
    assert status in audits[-1]["result"]


async def test_handle_reschedule_puts_status_and_note(env) -> None:
    backend, _ = env
    agent = _agent("handle_reschedule", {"request_id": 4, "status": 2, "admin_note": "时间冲突"})
    pending = await _run_until_confirm(agent)
    assert "拒绝改期申请 #4" in pending["summary"]
    await _resume(agent, {"decision": "approve", "fingerprint": pending["fingerprint"]})
    assert backend.calls[0][:3] == (
        "PUT",
        "/api/interview/reschedule/admin/4/handle",
        {"status": 2, "adminNote": "时间冲突"},
    )


async def test_invalid_reschedule_status_never_reaches_confirmation(env) -> None:
    backend, _ = env
    result = await write.handle_reschedule(request_id=4, status=3)
    assert result["status"] == "error" and backend.calls == []


# ── 装配:谁能看见写工具 ──────────────────────────────


def _identity(role: str, codes: list[str]) -> dict:
    return {"user_id": 8, "role": role, "role_names": [], "permission_codes": codes,
            "source": "web", "name": None}


def test_write_tools_need_allow_write_admin_and_permission() -> None:
    admin = _identity("admin", ["interview:schedule"])
    assert "assign_interview" in tool_roster(admin, allow_write=True)
    assert "assign_interview" not in tool_roster(admin)  # 未开 allow_write(CLI/eval)
    assert "assign_interview" not in tool_roster(
        _identity("admin", ["kb:manage"]), allow_write=True
    )
    assert "assign_interview" not in tool_roster(
        _identity("member", ["interview:schedule"]), allow_write=True
    )
    names = [t.__name__ for t in assemble_tools(admin, "jwt", allow_write=True)]
    assert names == tool_roster(admin, allow_write=True)  # 名单与装配一致


def test_contract_explains_confirmation_semantics() -> None:
    contract = tool_contract(_identity("admin", ["resume:audit"]), allow_write=True)
    assert "assign_interview" in contract and "确认卡片" in contract and "cancelled" in contract
