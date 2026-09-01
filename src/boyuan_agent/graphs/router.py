"""A 模块前门(ADR-0003):身份解析 → 按角色装配工具集 → ReAct 单循环。

意图分类节点已按 ADR-0003 移除:「意图」由模型在单循环内选工具隐式表达;
写操作安全由三层承担——工具装配(读不到)+ interrupt 确认(执行不了)+
令牌指纹校验(绕不过,ADR-0005)。B 由调度器直连 evaluation 图、C 由宿主
界面直连 copilot 图,均不经本前门。

凭证红线(GRA-01):凭证在入口层经 graphs/identity.py 解析,只有解析结果
(user_id/role/permission_codes)进 state;凭证绝不进 state/checkpointer。
resolve_identity 节点做确定性校验与规范化(unknown 兜底),不做网络调用。
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from boyuan_agent.graphs.identity import Role

_VALID_ROLES: tuple[Role, ...] = ("admin", "member", "candidate", "unknown")


class AgentState(TypedDict, total=False):
    """A 前门状态。"""

    messages: list  # LangChain message 序列
    user_id: int | None
    role: Role
    permission_codes: list[str]  # SEC-02 装配输入


def resolve_identity(state: AgentState) -> AgentState:
    """确定性节点(GRA-01):校验入口层已解析的身份,规范化为装配可用形态。

    身份解析本体在入口层(graphs/identity.resolve):官网 JWT 走
    GET /api/auth/me(SEC-01 落地后),CLI 模拟身份走 login+claims。
    本节点只做三件事:user_id 缺失→unknown 降级;role 非法→unknown;
    permission_codes 缺省→空列表(装配层对 unknown/空集只给最小只读)。
    """
    role = state.get("role", "unknown")
    if role not in _VALID_ROLES:
        role = "unknown"
    if state.get("user_id") is None:
        role = "unknown"
    return {
        "user_id": state.get("user_id"),
        "role": role,
        "permission_codes": state.get("permission_codes") or [],
    }


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
