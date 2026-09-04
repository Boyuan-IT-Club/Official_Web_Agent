"""GRA-05 写操作 interrupt 全流程测试:挂起→批准/拒绝→恢复。

用真实 LangGraph 图 + fake 写工具验证:
- 模型调用写工具 → 工具内 interrupt() 挂起(带操作摘要)
- Command(resume="approve") → 批准 → 工具执行
- Command(resume="reject") → 拒绝 → 工具取消
- 非图上下文直接调工具 → ConfirmationRequired(写路径 fail-closed)
"""

from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from official_agent.tools.interrupt_guard import ConfirmationRequired, require_confirmation

executed: list[str] = []


@tool
def fake_assign(resume_id: int, target_session_id: int) -> str:
    """模拟写操作:把候选人分配到场次,须人工确认。"""
    decision = require_confirmation(
        f"将把简历 #{resume_id} 分配到场次 #{target_session_id},请确认"
    )
    if decision == "approve":
        executed.append(f"assign {resume_id}->{target_session_id}")
        return f"已分配简历 #{resume_id} 到场次 #{target_session_id}"
    executed.append(f"cancel {resume_id}->{target_session_id}")
    return f"已取消分配简历 #{resume_id}"


class _FakeModel(BaseChatModel):
    """fake 模型:总是调用 fake_assign。"""

    @property
    def _llm_type(self) -> str:
        return "fake-tool-calling"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "_FakeModel":  # noqa: ARG002
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "fake_assign",
                                "args": {"resume_id": 1, "target_session_id": 2},
                                "id": "c1",
                            }
                        ],
                    )
                )
            ]
        )


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _build_app() -> Any:
    model = _FakeModel()
    graph = StateGraph(_State)
    graph.add_node("model", lambda s: {"messages": [model.invoke(s["messages"])]})
    graph.add_node("tools", ToolNode([fake_assign]))
    graph.add_edge(START, "model")
    graph.add_edge("model", "tools")
    graph.add_edge("tools", END)
    return graph.compile(checkpointer=MemorySaver())


@pytest.fixture(autouse=True)
def _clear_executed() -> None:
    executed.clear()


@pytest.mark.asyncio
async def test_interrupt_suspends_with_summary() -> None:
    """写工具调用挂起图,interrupt 携带操作摘要。"""
    app = _build_app()
    cfg = {"configurable": {"thread_id": "t1"}}
    out = await app.ainvoke({"messages": [HumanMessage("帮我分配")]}, cfg)
    interrupts = out.get("__interrupt__", [])
    assert len(interrupts) == 1
    assert "简历 #1" in str(interrupts[0].value)
    assert "请确认" in str(interrupts[0].value)
    assert executed == []  # 未确认不执行


@pytest.mark.asyncio
async def test_approve_resume_executes() -> None:
    """批准后工具执行,结果回报。"""
    app = _build_app()
    cfg = {"configurable": {"thread_id": "t2"}}
    await app.ainvoke({"messages": [HumanMessage("帮我分配")]}, cfg)
    out = await app.ainvoke(Command(resume="approve"), cfg)
    assert executed == ["assign 1->2"]
    last = out["messages"][-1]
    assert "已分配简历 #1" in str(last.content)


@pytest.mark.asyncio
async def test_reject_resume_cancels() -> None:
    """拒绝后工具取消,不执行。"""
    app = _build_app()
    cfg = {"configurable": {"thread_id": "t3"}}
    await app.ainvoke({"messages": [HumanMessage("帮我分配")]}, cfg)
    out = await app.ainvoke(Command(resume="reject"), cfg)
    assert executed == ["cancel 1->2"]
    last = out["messages"][-1]
    assert "已取消" in str(last.content)


@pytest.mark.asyncio
async def test_write_tool_outside_graph_fails_closed() -> None:
    """非图上下文直接调写工具 → ConfirmationRequired(写路径 fail-closed)。"""
    from official_agent.tools.write import assign_interview

    with pytest.raises(ConfirmationRequired):
        await assign_interview(resume_id=1, target_session_id=2)


@pytest.mark.asyncio
async def test_invalid_decision_rejected() -> None:
    """非法确认决策被拒。"""
    app = _build_app()
    cfg = {"configurable": {"thread_id": "t4"}}
    await app.ainvoke({"messages": [HumanMessage("帮我分配")]}, cfg)
    with pytest.raises(ConfirmationRequired):
        await app.ainvoke(Command(resume="maybe"), cfg)