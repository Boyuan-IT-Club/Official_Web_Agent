"""TOOL-02 MCP Server 测试:注册面精确性 + 协议级调用 + 双 transport。

安全边界断言(ADR-0003):MCP 只暴露只读工具——
写工具与 get_my_interview(需最终用户令牌)绝不出现,这是权限边界,不是约定。
"""

import asyncio
import pathlib

import httpx
import pytest
import respx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError

from boyuan_agent.config import Settings
from boyuan_agent.tools import readonly
from boyuan_agent.tools.client import BackendClient

EXPECTED_TOOLS = {
    "get_open_cycle",
    "search_resumes",
    "get_resume_detail",
    "find_available_sessions",
    "list_unassigned",
    "list_reschedule_requests",
    "get_recruit_statistics",
    "get_candidate_card",
}
FORBIDDEN_TOOLS = {
    "get_my_interview",  # 需最终用户本人令牌,服务账号语义不适用
    "assign_interview",
    "handle_reschedule",
    "submit_resume_score",  # 写工具仅 agent 进程内经 interrupt 装配
}


@pytest.fixture
def mock_backend():
    settings = Settings(
        _env_file=None,
        backend_base_url="http://backend.test",
        backend_service_username="svc",
        backend_service_password="secret",
    )
    readonly.set_backend_client(
        BackendClient(http=httpx.AsyncClient(base_url="http://backend.test"), settings=settings)
    )
    yield
    readonly.set_backend_client(None)


async def test_exposes_exactly_the_eight_readonly_tools():
    from boyuan_agent.mcp_server import server

    names = {t.name for t in await server.list_tools()}
    assert names == EXPECTED_TOOLS
    assert not names & FORBIDDEN_TOOLS


async def test_every_tool_has_model_facing_description():
    """docstring 即模型看到的工具描述(ADR-0003)——缺失等于模型盲选。"""
    from boyuan_agent.mcp_server import server

    for tool in await server.list_tools():
        assert tool.description and len(tool.description.strip()) > 10, f"{tool.name} 缺描述"


async def test_required_params_present_in_schema():
    """cycle_id 等必填参数必须进 inputSchema.required,模型才不会盲调。"""
    from boyuan_agent.mcp_server import server

    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["find_available_sessions"].input_schema
    assert "cycle_id" in schema.get("required", [])
    assert "cycle_id" in tools["list_unassigned"].input_schema.get("required", [])


@respx.mock
async def test_call_tool_through_mcp_pipeline(mock_backend):
    """MCPServer.call_tool 全链路:参数校验→函数调用→结果序列化。"""
    respx.post("http://backend.test/api/auth/login").side_effect = httpx.Response(
        201, json={"code": 200, "message": "ok", "data": {"token": "tok", "user_id": 1}}
    )
    respx.get("http://backend.test/api/cycles/open").side_effect = httpx.Response(
        200, json={"code": 200, "message": "ok", "data": [{"cycleId": 2}]}
    )

    from boyuan_agent.mcp_server import server

    result = await server.call_tool("get_open_cycle", {})
    text = result.content[0].text
    assert "cycleId" in text
    assert not result.is_error


@respx.mock
async def test_call_tool_validates_arguments(mock_backend):
    """缺必填参数:MCP 层抛校验错误(SDK v2 语义),不打后端。"""
    from boyuan_agent.mcp_server import server

    with pytest.raises(ToolError, match="cycle_id"):
        await server.call_tool("find_available_sessions", {})


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


async def test_stdio_transport_protocol_handshake():
    """真 stdio 协议级冒烟:子进程起 server,ClientSession 握手并 list_tools。"""
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "boyuan_agent.mcp_server"],
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await asyncio.wait_for(session.initialize(), timeout=60)
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert names == EXPECTED_TOOLS
        assert not names & FORBIDDEN_TOOLS


async def test_http_transport_protocol_handshake():
    """--http 路线的协议级验证:uvicorn 起 streamable-http app,client 握手+list。"""
    import socket

    import uvicorn

    from boyuan_agent.mcp_server import server

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(
        server.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    httpd = uvicorn.Server(config)

    server_task = asyncio.get_running_loop().create_task(httpd.serve())
    try:
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await asyncio.wait_for(session.initialize(), timeout=15)
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert names == EXPECTED_TOOLS
            assert not names & FORBIDDEN_TOOLS
    finally:
        httpd.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
