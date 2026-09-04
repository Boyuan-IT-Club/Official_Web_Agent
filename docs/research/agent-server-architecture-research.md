# 客服 Agent 服务端架构调研参考(INF-04 决策输入)

> 调研日期:2026-09-03。来源:LangGraph 官方文档 / 社区生产实践文章 / Ably 会话层文章。
> 用途:为 #91(Agent FastAPI 服务骨架 INF-04)决策提供事实,落盘后与 Zewang 决策。

## 结论速览

- **LangGraph 的权威做法是"编译一次、共享 graph、线程隔离会话"**,不是"每会话新建 agent"。
  会话 = `thread_id` 维度,不是进程对象维度。
- 单进程 async(`ainvoke`)即可服务数百并发会话;多 worker/多副本是可选的水平扩展,
  前提是按 `thread_id` 会话粘性(checkpointer 共享)。
- checkpointer 是跨进程共享的会话记忆权威源;**agent 持有的是可变工具/客户端状态**,
  需要按会话隔离的是**client(token)**,不是 checkpointer。
- 直接 SSE 在**重连/跨端/取消**上会碎(Ably),生产需要"会话层";但官网 M1 单端单设备
  场景,会话层是过度设计,普通 POST+SSE 足够——会话层留给 M3+。

## 1. LangGraph 官方:checkpointer 与 thread 模型

来源:https://docs.langchain.com/oss/python/langgraph/checkpointers

- checkpointer 把图状态按 **thread** 组织,每个 checkpoint 存一条超步快照。
- 用 checkpointer 编译的图,invoke 必须带 `thread_id`:
  ```python
  config = {"configurable": {"thread_id": "1"}}
  graph.invoke({...}, config)
  ```
- thread 是 checkpointer 的主键;会话记忆 = 同 `thread_id` 追加消息 → 天然隔离不同会话。
- checkpointer 三种持久化模式:sync(每步同步落盘,最稳)/ async(异步,进程崩溃可能丢最近一步)/ exit(退出才落盘)。
- 官方提供 Postgres 实现(`AsyncPostgresSaver`),我们已在用(state/pg.py)。**Redis 是可选替代,不是必须**。

## 2. 社区生产实践:千并发会话怎么搭

来源:https://markaicode.com/howto/how-to-scale-ai-agents/(2026-08-15,验证于 LangGraph 1.2.11)

**关键模式:编译一次,共享 graph,`thread_id` 隔离。**

```python
async def init_graph():
    await checkpointer.asetup()
    return builder.compile(checkpointer=checkpointer)  # 编译一次,进程级复用

async def run_session(graph, session_id: str):
    config = {"configurable": {"thread_id": session_id}}
    await graph.ainvoke({"messages": [], ...}, config)  # 并发用 ainvoke
```

- **单进程并发**:`ainvoke` 在 `asyncio.gather` 里跑,一个进程能同时 hold 上百个会话
  (受 LLM 等待而非 CPU 限制)。所以我们**不用多 worker 也能服务大量并发**。
- **进程重启不丢会话**:靠 checkpointer(Postgres/Redis 落盘)。用户重连带原 `thread_id`
  即恢复上下文。
- **水平扩展(多 worker/多副本)**:进程是**无会话状态**的,只有 checkpointer(共享存储)
  是权威;负载均衡按 `thread_id` 粘性或每请求重建都可。**目标是"无状态 worker +
  共享 checkpointer"**,不是"要点名某进程"。
- **限流**:并发多会话会撞 LLM 供应商限流,用 token-bucket 限流器。
- **版本注意**:必须用 `Async*Saver`(async),sync saver 配 `ainvoke` 是常见坑;Redis
  8+ 才内置 JSON/Search 模块(我们用 Postgres 无此问题)。
- **编者加注**:千并发是"要服务 1000 个会话"的水平。我们官网候选人数远小于此,
  **完全用不到 Redis/多副本**。

## 3. Ably:为什么"会话层"是生产刚需(但官网 M1 用不到)

来源:https://ably.com/blog/production-ai-session-layer-vs-http-streaming(2026-04)

- **直接 HTTP SSE 的碎点**(针对客服级产品):
  - **断线重连**:移动端断网 → token 找不到地方去 → 要缓冲+重放;而 SSE 是单向的,
    客户端没法在重连时把"我丢到哪了"告诉服务端。
  - **跨端**:SSE 连接是"一个客户端↔一个 agent"的私有管道,别的 tab/手机看不到。
  - **取消/重定向**:SSE 只有"关连接"一个手段,服务端分不清"用户取消"还是"断网"。
- **生产模式=会话层**:一个**独立于连接的持久会话**,agent 往会话发布事件,客户端订阅并
  从最后一条续传;多端同时看同一会话;取消/重定向有显式信号(不是 TCP 副作用)。
- **对本项目的判断**:官网浮窗是**单端、单设备、一人一窗**;断线是用户自己断、重新发一条
  即可(我们已支持 `session_id` 续传)。会话层(Redis 缓冲/事件重放/多端同步)是 **M3+
  多端/中断恢复** 才需要的复杂度,非本期。

## 4. 对本项目骨架的推论

| 决定点 | 社区做法 | 本项目建议 | 理由 |
|---|---|---|---|
| agent 复用 | **编译一次、共享 graph** | **同** | checkpointer(thread_id)管记忆,agent 无会话态 |
| 每会话独立 | 独立的是 **client(token)** | **同** | `get_my_interview` 用用户 JWT,进程级单例会串租户(client.py get_as_user) |
| checkpointer | 共享 AsyncPostgresSaver | **同** | 已是现状(state/pg.py);跨进程唯一权威 |
| worker 数 | 单进程 async 足矣,多副本可选 | **单 worker 起步** | 官网量级远低于千并发;多副本留 INF-11 |
| SSE 形状 | 会话层(重连/跨端/取消) | **POST + SSE 流** | M1 单端单设备;`session_id` 续传已覆盖断线 |
| 每会话 agent | 不新建 | **共享 graph(修订)** | 见 §1;新建是误解,浪费编译与内存 |
| 限流 | token-bucket | 本期不做,INF-11 | 官网量级触发不到限流 |
| PG vs Redis | Postgres 够,Redis 是可选 | **Postgres(现状)** | ADR-0007;我们已用 AsyncPostgresSaver |

## 5. 待 Zewang 确认

调研修正了 Q2 推荐的"每会话独立 agent"——**正确做法是共享编译好的 graph,独立的是
按会话持有的 client(token)**。请确认按此方向落 INF-04。