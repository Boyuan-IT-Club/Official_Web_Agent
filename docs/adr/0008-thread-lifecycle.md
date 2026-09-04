# ADR-0008: Thread 生命周期契约(SEC-07)

- **Status**: Accepted(2026-09-03)
- **领域**:Thread(会话)的诞生/属主/并发/终结/跨入口语义
- **依赖**:ADR-0007(checkpointer 存储)、MEM-01(Postgres checkpointer 已落地)、SEC-06(数据留存,TTL 拍板后对齐)

## 背景

会话是 A 模块对话的最小单位:一次对话 = 一个 thread_id,历史由 Postgres checkpointer
(ADR-0007)按 thread_id 持久化。SEC-07 定义 thread 从生到死的契约,回答:
thread_id 长什么样、谁能访问、并发怎么串行、何时终结、跨入口如何隔离。

## 决策

### 1. 诞生:thread_id 命名 = `{入口}:{用户标识}:{random8}`

```
cli:u7:a3f9c2d1      # CLI 入口,后端 user_id=7
web:u42:8e1b7f0a     # 官网入口,user_id=42
feishu:o6b3a2:5c9d   # 飞书入口,open_id 前 6 位(防 PII 全量入库)
```

- **入口**:`cli` / `web` / `feishu`。MCP 是维护态,不作入口(ADR-0003)。
- **用户标识**:线程自带属主维度,可枚举性消失——猜中别人 thread_id 也无法通过
  属主校验。
- **random8**:`secrets.token_hex(4)`,防碰撞。
- **subject(别名)**:用户侧会话名(如 CLI `-s qa-1`)存 `agent_threads.subject` 列,
  **不拼进 thread_id**。thread_id 不含任何用户可控串。

### 2. 属主:恢复/读取路径硬校验

- **创建**:建档写 `owner_user_id`(已实现)。
- **恢复/读取**:`resolve_thread(thread_id, actor_user_id)` 硬校验——
  非属主一律返回 None(403 语义)。调用方(CLI 恢复历史、GRA-05 恢复确认令牌、
  未来 SSE/MCP)统一走此入口,防"可枚举 = 能翻别人会话 = PII 泄露"。
- `soft_delete_thread` 的 owner 为必填参数(M-3 修复),杜绝跨属主误删。

### 3. 并发:同 thread 消息串行化

- **问题**:飞书连发两指令/双端同开 → checkpointer 写冲突 + 确认令牌错配。
- **v1**:确认令牌绑定 `thread_id + 操作指纹`(ADR-0005);同 thread 的写操作路径
  用 PG `pg_try_advisory_xact_lock` 串行化(GRA-05 落地)。
- CLI 单用户无并发,此约束主要约束飞书/官网入口的并发写。

### 4. 终结:软删除 + TTL 待定

- **软删除**:`status='terminated' + deleted_at`(已实现)。
- **续接已终结 thread**:**拒绝**(本 ADR 拍板:终结即终结,不复活保留历史)。
  `find_active_by_subject` 只查 `status='active'`,软删后同名别名的续接失效。
- **TTL 清理**:待 SEC-06 拍板后加后台任务;挂起确认载荷(interrupt 状态)按 PII,
  与 thread 同生命周期删除。

### 5. 跨入口:同一用户换入口 = 新 thread

- v1 契约:**换入口不共享历史**。用户 CLI 问完 → 官网再问 = 新 thread。
- 理由:checkpointer 恢复依赖 thread_id,跨入口共享会串上下文(不同工具集/身份
  强度);SEC-06 留存未定,不宜跨入口共享。
- C 模块(copilot)的 thread 切分(面试官×场次)在 COP-04 内引用本契约。

## 影响

- `agent_threads` 加 `subject` 列(兼容旧 DDL 的 IF NOT EXISTS 自举)。
- CLI `-s` 语义从"裸 thread_id"改为"会话别名":同名续接该用户最近 active 线程,
  无则新开;不传则每次新会话(MEM-01 的 `--session dev` 无状态模式废弃)。
- 旧测试数据(无真实用户)直接清理,无迁移成本。

## 开放问题

- SEC-06 定 TTL 后:清理任务 + 挂起载荷处置落落地。
- 飞书入口落地后:open_id 截断长度的最终确认(6 位是否够防碰撞)。