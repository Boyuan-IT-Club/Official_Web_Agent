"""MEM-01 真库验证:checkpointer 建表 + thread 建档 + 真实图两轮持久化。

用 LangGraph StateGraph(最小 echo 图)+ AsyncPostgresSaver,跑两轮:
- 第一轮写入消息
- 同一 thread_id,新连接(模拟重启)第二轮读回历史并追加
证明跨连接持久化成立。

同时覆盖 H-3(同 tid 二次建档幂等)与 L-1(ensure_agent_threads_table 自举)。
"""

import asyncio
import sys

sys.path.insert(0, "src")

from typing import Annotated

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from boyuan_agent.state.pg import get_checkpointer
from boyuan_agent.state.threads import (
    create_thread,
    ensure_agent_threads_table,
    get_thread,
    soft_delete_thread,
)


class State(TypedDict):
    messages: Annotated[list, add_messages]


async def main() -> None:
    # 0. 自举:幂等建 agent_threads 档案表(L-1,新环境可跑)
    ensure_agent_threads_table()
    print("[0] ensure_agent_threads_table OK")

    # 1. checkpointer 建表
    async with get_checkpointer() as saver:
        assert isinstance(saver, AsyncPostgresSaver)
        print("[1] get_checkpointer() OK")

    # 2. 建档 + H-3 幂等:同 tid 二次建档不炸
    rec = create_thread("cli", 7, subject="mem01-e2e-verify")
    print(f"[2] 建档 OK thread_id={rec.thread_id} subject={rec.subject}")
    rec2 = create_thread("cli", 7, thread_id=rec.thread_id)
    assert rec2.thread_id == rec.thread_id
    print("[2b] H-3 同 tid 二次建档幂等 OK")
    assert get_thread(rec.thread_id) is not None
    print("[3] get_thread OK")

    # 3. 最小真实图:echo 节点
    def echo(state: State) -> dict:
        return {"messages": [HumanMessage(content="`echo` 节点回执")]}

    graph = StateGraph(State)
    graph.add_node("echo", echo)
    graph.set_entry_point("echo")
    graph.set_finish_point("echo")

    config = {"configurable": {"thread_id": rec.thread_id}}

    # 第一轮(连接 1)
    async with get_checkpointer() as saver:
        app = graph.compile(checkpointer=saver)
        await app.ainvoke({"messages": [HumanMessage(content="第一轮:你好")]}, config=config)
        print("[4] 第一轮执行 OK")

    # 第二轮(连接 2,模拟重启)
    async with get_checkpointer() as saver:
        app = graph.compile(checkpointer=saver)
        state = await app.aget_state(config)
        assert state is not None
        msgs = state.values["messages"]
        assert any("第一轮" in str(m.content) for m in msgs), f"历史未持久化:{msgs}"
        print(f"[5] 第二轮读回 OK:历史 {len(msgs)} 条,含第一轮消息")

        # 追加第二轮
        await app.ainvoke({"messages": [HumanMessage(content="第二轮:还在吗")]}, config=config)
        state2 = await app.aget_state(config)
        msgs2 = state2.values["messages"]
        assert any("第二轮" in str(m.content) for m in msgs2)
        print(f"[6] 第二轮追加 OK:共 {len(msgs2)} 条")

    # 4. 软删除
    assert soft_delete_thread(rec.thread_id, owner_user_id=7)
    deleted = get_thread(rec.thread_id)
    assert deleted is not None and deleted.status == "terminated" and deleted.deleted_at is not None
    print("[7] 软删除 OK")

    print("\n=== MEM-01 真库验证全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())