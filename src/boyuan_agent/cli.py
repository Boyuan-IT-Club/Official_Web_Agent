"""CLI 对话入口(INF-03):开发调试主力,M1 验收载体。

链路:模拟身份凭证 → GRA-01 身份解析 → GRA-04 agent 工厂 → 流式多轮对话。
身份信息作为首条用户消息注入(静态前缀纪律);多轮历史本地累积
(checkpointer 接线是 MEM-01,落地前 session 仅作 thread_id 标识);
Langfuse callbacks fail-open 挂载(OBS-01)。

用法:
    uv run boyuan-agent chat                       # .env 服务账号身份
    uv run boyuan-agent chat --username admin      # 模拟指定账号(密码交互输入)
    uv run boyuan-agent chat --session recruit-qa  # 会话 id(trace/thread 标识)
"""

import asyncio

import httpx
import typer
from langchain_core.messages import AIMessageChunk, HumanMessage
from rich.console import Console

from boyuan_agent.graphs.assistant import (
    assemble_tools,
    build_assistant_agent,
    identity_message,
)
from boyuan_agent.graphs.identity import resolve
from boyuan_agent.observability import langfuse_callbacks
from boyuan_agent.tools.client import BackendError
from boyuan_agent.tools.readonly import get_backend_client

app = typer.Typer(help="博远招新 Agent 开发 CLI")
console = Console()

_EXIT_WORDS = {"exit", "quit", "退出", "q"}


@app.command()
def chat(
    username: str = typer.Option(
        "", "--username", "-u", help="模拟身份账号(空=用 .env 服务账号)"
    ),
    password: str = typer.Option("", "--password", "-p", help="密码(空且指定了账号则交互输入)"),
    session: str = typer.Option("dev", "--session", "-s", help="会话 id(thread/trace 标识)"),
) -> None:
    """本地多轮对话,流式输出,支持指定模拟身份。"""
    if username and not password:
        password = typer.prompt(f"账号 {username} 的密码", hide_input=True)
    asyncio.run(_chat(username, password, session))


async def _chat(username: str, password: str, session: str) -> None:
    try:
        identity = await resolve(
            {"kind": "cli", "username": username, "password": password}  # type: ignore[typeddict-item]
            if username
            else {"kind": "cli"}
        )
    except (BackendError, httpx.HTTPError) as exc:
        # 后端不可达/网关错也走人话,不裸栈(review Inf03Review 实测)
        console.print(f"[red]身份解析失败:[/red] {exc}")
        console.print("[yellow]提示: 确认后端已启动且 .env 的 BACKEND_BASE_URL 正确[/yellow]")
        raise typer.Exit(1) from None

    user_token = (await get_backend_client()).token or ""
    try:
        agent = build_assistant_agent(identity, user_token=user_token)
    except Exception as exc:  # noqa: BLE001 — 入口层:模型缺 key 等配置错误转人话
        console.print(f"[red]模型初始化失败:[/red] {exc}")
        console.print("[yellow]提示: 需要在 .env 配置 ANTHROPIC_API_KEY[/yellow]")
        raise typer.Exit(1) from None
    callbacks = langfuse_callbacks()

    console.print(
        f"[bold]boyuan-agent[/bold] 身份={identity['role']}(用户 {identity['user_id']}) "
        f"session={session} 工具={len(assemble_tools(identity, user_token))}个 "
        f"exit/退出 结束"
    )
    # 静态前缀纪律:身份是首条用户消息,不进 system
    history: list = [HumanMessage(content=identity_message(identity))]

    while True:
        try:
            user_input = console.input("[bold cyan]你>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]再见[/dim]")
            return
        if not user_input:
            continue
        if user_input.lower() in _EXIT_WORDS:
            console.print("[dim]再见[/dim]")
            return

        history.append(HumanMessage(content=user_input))
        try:
            history = await _run_turn(agent, history, session, callbacks)
        except KeyboardInterrupt:
            console.print("\n[dim]已中断本轮(历史保留)[/dim]")
            continue
        except Exception as exc:  # noqa: BLE001 — 入口层:模型/网络错误转人话,会话不崩
            console.print(f"\n[red]本轮执行失败:[/red] {exc}")
            if "api_key" in str(exc).lower() or "anthropic" in str(exc).lower():
                console.print("[yellow]提示: .env 需配置 ANTHROPIC_API_KEY[/yellow]")
            history.pop()  # 失败轮不进历史,防脏上下文


async def _run_turn(agent: object, history: list, session: str, callbacks: list) -> list:
    """跑一轮:流式打印 token 与工具状态,返回更新后的完整消息历史。"""
    console.print("[bold green]agent>[/bold green] ", end="")
    config = {
        "callbacks": callbacks,
        "configurable": {"thread_id": session},  # MEM-01 checkpointer 落地后生效
    }
    new_messages: list = []  # 本轮增量(create_agent 的 updates 每节点只吐新增)
    async for mode, payload in agent.astream(  # type: ignore[attr-defined]
        {"messages": history}, config=config, stream_mode=["messages", "updates"]
    ):
        if mode == "messages":
            chunk, _meta = payload
            if isinstance(chunk, AIMessageChunk):
                if chunk.content:
                    console.print(chunk.content, end="", markup=False, highlight=False)
                # 工具调用状态:参数块到达时显示工具名
                for tc in chunk.tool_call_chunks or []:
                    if tc.get("name"):
                        console.print(f"\n[dim]→ 调用 {tc['name']}…[/dim] ", end="")
        elif mode == "updates":
            for _ns, node_update in payload.items():
                if isinstance(node_update, dict):
                    new_messages.extend(node_update.get("messages") or [])
    console.print()
    # 增量累积:历史=原历史+本轮全部节点新增;空消息过滤防呆。
    # 勿用末节点整体替换——真实图每节点只吐增量,替换会丢身份与提问
    # (review Inf03Review 实测抓出的缺陷)。
    meaningful = [
        m
        for m in new_messages
        if getattr(m, "content", "")
        or getattr(m, "tool_calls", None)
        or getattr(m, "tool_call_id", None)
    ]
    return [*history, *meaningful]
