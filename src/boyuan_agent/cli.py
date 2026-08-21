"""CLI 对话入口(INF-03):开发调试主力,支持模拟身份。"""

import typer
from rich.console import Console

app = typer.Typer(help="博远招新 Agent 开发 CLI")
console = Console()


@app.command()
def chat(
    role: str = typer.Option("admin", help="模拟身份:admin / candidate"),
    session: str = typer.Option("dev", help="会话 id(checkpointer 线程)"),
) -> None:
    """本地多轮对话,流式输出。"""
    # TODO(INF-03): 接 router 主图,流式打印 token 与工具调用状态
    console.print(f"[bold]boyuan-agent[/bold] role={role} session={session}")
    console.print("[yellow]主图尚未实现(GRA-01/02),见 issues。[/yellow]")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
