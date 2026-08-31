"""A 模块前门(ADR-0003):身份解析 → 按角色装配工具集 → ReAct 单循环。

意图分类节点已按 ADR-0003 移除:「意图」由模型在单循环内选工具隐式表达;
写操作安全由三层承担——工具装配(读不到)+ interrupt 确认(执行不了)+
令牌指纹校验(绕不过,ADR-0005)。B 由调度器直连 evaluation 图、C 由宿主
界面直连 copilot 图,均不经本前门。
"""

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict, total=False):
    """A 前门状态。"""

    messages: list  # LangChain message 序列
    user_id: int | None
    role: Literal["candidate", "admin", "unknown"]


def resolve_identity(state: AgentState) -> AgentState:
    """确定性节点(GRA-01):飞书 open_id / 官网 JWT / CLI 模拟身份 → 后端用户与角色。

    官网 JWT 走 GET /api/auth/me(ADR-0006,SEC-01 后端增量),agent 不持有 JWT_SECRET。
    """
    raise NotImplementedError("GRA-01")


def assistant_loop(state: AgentState) -> AgentState:
    """ReAct 单循环(GRA-04):按 role 装配工具集(SEC-02),模型自主选工具。

    写工具调用触发 interrupt 挂起,确认令牌机制见 ADR-0005 与 tools/write.py。
    """
    raise NotImplementedError("GRA-04")


def build_router_graph() -> StateGraph:
    graph = StateGraph(AgentState)
    graph.add_node("resolve_identity", resolve_identity)
    graph.add_node("assistant_loop", assistant_loop)
    graph.add_edge(START, "resolve_identity")
    graph.add_edge("resolve_identity", "assistant_loop")
    graph.add_edge("assistant_loop", END)
    return graph
