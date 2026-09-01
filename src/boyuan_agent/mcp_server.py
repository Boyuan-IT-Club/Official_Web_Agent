"""MCP Server(TOOL-02):工具层的纯对外暴露面(ADR-0003)。

仅供外部消费者(Claude Code 等)挂载:
- stdio(默认,本地 Claude Code):`uv run python -m boyuan_agent.mcp_server`
- HTTP:`uv run python -m boyuan_agent.mcp_server --http`(127.0.0.1:8000,/mcp)
agent 进程内不走 MCP 回环——直接绑定 tools/ 下的 Python 函数(TOOL-07)。

依赖 mcp SDK v2(FastMCP 已改名 MCPServer,transport 参数移至 run)。
"""
import sys
from typing import Literal

from mcp.server import MCPServer

from boyuan_agent.tools import readonly

server = MCPServer(
    "boyuan-backend",
    instructions=(
        "博远信息技术社招新后端的只读查询工具集:招募周期、简历检索与详情、"
        "面试场次容量、未分配候选人、改期申请、汇总统计。"
        "多步查询通常先 get_open_cycle 拿 cycle_id。无写操作。"
    ),
)

# 只读工具注册。有意不注册的:
# - get_my_interview:需最终用户本人令牌,服务账号语义下无意义
# - 写工具(tools/write.py):仅在 agent 进程内经 interrupt 确认流程装配
#   (GRA-05),MCP 消费方无确认通道,暴露即越权
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
    server.add_tool(fn)


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    transport: Literal["stdio", "streamable-http"] = (
        "streamable-http" if "--http" in args else "stdio"
    )
    server.run(transport=transport)


if __name__ == "__main__":
    main()
