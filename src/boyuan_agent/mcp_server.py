"""MCP Server(TOOL-02):工具层的纯对外暴露面(ADR-0003)。

仅供外部消费者(Claude Code 等)挂载:
- stdio(默认,本地 Claude Code):`uv run python -m boyuan_agent.mcp_server`
- HTTP:`uv run python -m boyuan_agent.mcp_server --http`(127.0.0.1:8000,/mcp)
agent 进程内不走 MCP 回环——直接绑定 tools/ 下的 Python 函数(TOOL-07)。

依赖 mcp SDK v2(FastMCP 已改名 MCPServer,transport 参数移至 run)。
"""

import functools
import inspect
import sys
from collections.abc import Awaitable, Callable

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from boyuan_agent.tools import readonly
from boyuan_agent.tools.client import BackendError

server = MCPServer(
    "boyuan-backend",
    instructions=(
        "博远信息技术社招新后端的只读查询工具集:招募周期、简历检索与详情、"
        "面试场次容量、未分配候选人、改期申请、汇总统计。"
        "多步查询通常先 get_open_cycle 拿 cycle_id。无写操作。"
    ),
)


def _normalize_for_mcp(fn: Callable[..., Awaitable[object]]) -> Callable[..., Awaitable[object]]:
    """MCP 消费面规范化:list 包 dict + 业务错误文案透传。

    两处 SDK v2 语义适配(真链路测试逐个抓出):
    - SDK 对 list 逐项转 content 再拼接——空 list 产出空 content,消费端
      (Claude Code)无法区分「空结果」与「工具没说话」;包成 {count, results}
      走整体 JSON 序列化,count 让模型先看到规模。
    - BackendError 是普通异常,SDK 会当 crash 包成 generic 的
      UnexpectedToolError——可行动文案被丢弃(ADR-0005「模型只见映射文案」
      在 MCP 面失效)。转成 ToolError 保住文案(anticipated failure 语义)。
    仅作用于 MCP 对外面;agent 进程内直连(TOOL-07)保持原语义。
    """

    @functools.wraps(fn)
    async def wrapper(*args: object, **kwargs: object) -> object:
        try:
            result = await fn(*args, **kwargs)
        except BackendError as exc:
            raise ToolError(str(exc)) from None
        if isinstance(result, list):
            return {"count": len(result), "results": result}
        return result

    # add_tool 按签名生成参数 schema,wrapper 需继承原签名
    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


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
    server.add_tool(_normalize_for_mcp(fn))


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if "--http" not in args:
        server.run(transport="stdio")
        return
    def _flag_value(flag: str, default: str) -> str:
        if flag not in args:
            return default
        i = args.index(flag)
        if i + 1 >= len(args):  # 缺值裸 IndexError 防护(#74 review nit)
            raise SystemExit(f"{flag} 需要一个值,如: {flag} 0.0.0.0")
        return args[i + 1]

    host = _flag_value("--host", "127.0.0.1")
    port = int(_flag_value("--port", "8000"))
    server.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    main()
