"""版本化 prompt。

约定:
- 每个 prompt 一个 .md 文件,文件内首行注释标版本与变更原因
- 代码里只 load,不内联长 prompt 字符串
- 改 prompt = 改代码:走 PR,跑 eval 门禁(OBS-07)
- 外部内容(简历/飞书消息/转写)注入时必须框定为数据区(SEC-04)
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


def load(name: str) -> str:
    """按文件名(不含扩展名)加载 prompt。"""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
