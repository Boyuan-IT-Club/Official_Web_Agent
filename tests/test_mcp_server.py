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

from official_agent.config import Settings
from official_agent.tools import readonly
from official_agent.tools.client import BackendClient

EXPECTED_TOOLS = {
    "auth_status",
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
    from official_agent.mcp_server import server

    names = {t.name for t in await server.list_tools()}
    assert names == EXPECTED_TOOLS
    assert not names & FORBIDDEN_TOOLS


async def test_every_tool_has_model_facing_description():
    """docstring 即模型看到的工具描述(ADR-0003)——缺失等于模型盲选。"""
    from official_agent.mcp_server import server

    for tool in await server.list_tools():
        assert tool.description and len(tool.description.strip()) > 10, f"{tool.name} 缺描述"


async def test_required_params_present_in_schema():
    """cycle_id 等必填参数必须进 inputSchema.required,模型才不会盲调。"""
    from official_agent.mcp_server import server

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

    from official_agent.mcp_server import server

    result = await server.call_tool("get_open_cycle", {})
    text = result.content[0].text
    assert "cycleId" in text
    assert not result.is_error


@respx.mock
async def test_call_tool_validates_arguments(mock_backend):
    """缺必填参数:MCP 层抛校验错误(SDK v2 语义),不打后端。"""
    from official_agent.mcp_server import server

    with pytest.raises(ToolError, match="cycle_id"):
        await server.call_tool("find_available_sessions", {})


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


async def test_stdio_transport_protocol_handshake():
    """真 stdio 协议级冒烟:子进程起 server,ClientSession 握手并 list_tools。"""
    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "official_agent.mcp_server"],
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

    from official_agent.mcp_server import server

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


@respx.mock
async def test_empty_list_result_still_has_content(mock_backend):
    """SDK 对空 list 产出空 content(消费端视为工具没说话)——注册层包成
    {count:0, results:[]} 保证空结果也可见。真链路测试抓到的回归。"""
    respx.post("http://backend.test/api/auth/login").side_effect = httpx.Response(
        201, json={"code": 200, "message": "ok", "data": {"token": "tok", "user_id": 1}}
    )
    respx.get("http://backend.test/api/cycles/open").side_effect = httpx.Response(
        200, json={"code": 200, "message": "ok", "data": []}
    )

    from official_agent.mcp_server import server

    result = await server.call_tool("get_open_cycle", {})
    assert not result.is_error
    assert result.content, "空列表结果必须有 content"
    assert '"count": 0' in result.content[0].text


@respx.mock
async def test_nonempty_list_result_is_single_json_block(mock_backend):
    """非空 list 逐项转多块 content → 规范化为单块 {count, results}。"""
    respx.post("http://backend.test/api/auth/login").side_effect = httpx.Response(
        201, json={"code": 200, "message": "ok", "data": {"token": "tok", "user_id": 1}}
    )
    respx.get("http://backend.test/api/interview/admin/cycles/1/unassigned").side_effect = (
        httpx.Response(
            200, json={"code": 200, "message": "ok", "data": [{"userId": 3}, {"userId": 4}]}
        )
    )

    from official_agent.mcp_server import server

    result = await server.call_tool("list_unassigned", {"cycle_id": 1})
    assert len(result.content) == 1
    assert '"count": 2' in result.content[0].text


async def test_wrapper_preserves_signature_for_schema():
    """wrapper 必须继承原签名,否则 add_tool 生成的参数 schema 退化。"""
    from official_agent.mcp_server import server

    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["find_available_sessions"].input_schema
    assert "cycle_id" in schema.get("required", [])
    assert "date" in schema.get("properties", {})


@respx.mock
async def test_backend_error_message_survives_to_model(mock_backend):
    """真链路抓到的回归:BackendError 被 SDK 当 crash 包成 generic 错误,
    可行动文案丢失(ADR-0005)——wrapper 转 ToolError 保住文案。"""
    respx.post("http://backend.test/api/auth/login").side_effect = httpx.Response(
        201, json={"code": 200, "message": "ok", "data": {"token": "tok", "user_id": 1}}
    )
    respx.get("http://backend.test/api/resumes/admin/1/1").side_effect = httpx.Response(
        404, json={"code": 3001, "message": "简历不存在"}
    )

    from official_agent.mcp_server import server

    # 进程内便捷方法抛 ToolError(SDK v2 语义);协议路径(session.call_tool)
    # 将其转成 is_error result 且文案进 content——两条路径文案都必须保住
    with pytest.raises(ToolError) as exc_info:
        await server.call_tool("get_resume_detail", {"user_id": 1, "cycle_id": 1})
    assert "简历不存在" in str(exc_info.value)
    assert "search_resumes" in str(exc_info.value)  # 可行动指引必须到达模型


def test_main_flag_without_value_fails_gracefully(capsys) -> None:
    """#74 review nit 回归:--host 缺值不裸 IndexError。"""
    from official_agent.mcp_server import main

    with pytest.raises(SystemExit, match="--host"):
        main(["--http", "--host"])
    with pytest.raises(SystemExit, match="--port"):
        main(["--http", "--port"])
