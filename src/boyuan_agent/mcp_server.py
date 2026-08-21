"""MCP Server(TOOL-02):把工具层对外暴露。

用法:
- 本地 Claude Code 挂载(stdio):`uv run python -m boyuan_agent.mcp_server`
- agent 内部经 langchain-mcp-adapters 消费同一套工具(TOOL-07)
"""

from mcp.server.fastmcp import FastMCP

from boyuan_agent.tools import readonly

mcp = FastMCP("boyuan-backend")

# 只读工具逐个注册;写工具(tools/write.py)仅在 agent 内部装配,不经 MCP 对外暴露
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
