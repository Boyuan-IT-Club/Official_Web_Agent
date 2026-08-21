"""Router 主图(GRA-01/02):身份解析 → 意图分类 → 子图分发。"""

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict, total=False):
    """主图状态。子图各自扩展,进子图前做上下文隔离。"""

    messages: list  # LangChain message 序列
    user_id: int | None
    role: Literal["candidate", "admin", "unknown"]
    intent: Literal["qa", "query", "write", "report"] | None


def resolve_identity(state: AgentState) -> AgentState:
    """确定性节点(GRA-01):飞书 open_id / 官网 JWT / CLI 模拟身份 → 后端用户与角色。"""
    raise NotImplementedError("GRA-01")


def classify_intent(state: AgentState) -> AgentState:
    """轻量模型节点(GRA-02):问答 / 查询 / 写操作 / 报表,低置信时追问澄清。"""
    raise NotImplementedError("GRA-02")


def build_router_graph() -> StateGraph:
    """组装主图。子图在各里程碑接入(GRA-03..06)。"""
    graph = StateGraph(AgentState)
    graph.add_node("resolve_identity", resolve_identity)
    graph.add_node("classify_intent", classify_intent)
    graph.add_edge(START, "resolve_identity")
    graph.add_edge("resolve_identity", "classify_intent")
    # TODO(GRA-03..06): add_conditional_edges 按 role+intent 分发到子图
    graph.add_edge("classify_intent", END)
    return graph
