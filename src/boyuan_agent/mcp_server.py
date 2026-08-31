"""MCP Server(TOOL-02):工具层的纯对外暴露面(ADR-0003)。

仅供外部消费者(Claude Code 等)挂载:`uv run python -m boyuan_agent.mcp_server`。
agent 进程内不走 MCP 回环——直接绑定 tools/ 下的 Python 函数(TOOL-07)。
"""

from mcp.server.fastmcp import FastMCP

from boyuan_agent.tools import readonly

mcp = FastMCP("boyuan-backend")

# 只读工具注册。有意不注册的:
# - get_my_interview:需最终用户本人令牌,服务账号语义下无意义
# - 写工具(tools/write.py):仅在 agent 进程内经 interrupt 确认流程装配
for fn in (
    readonly.get_open_cycle,
    readonly.search_resumes,
    readonly.get_resume_detail,
    readonly.find_available_sessions,
    readonly.list_unassigned,
    readonly.list_reschedule_requests,
    readonly.get_recruit_statistics,
    readonly.get_candidate_card,
):
    mcp.tool()(fn)


if __name__ == "__main__":
    mcp.run()
