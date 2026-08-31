# ADR-0007: Checkpointer 存储选型——倾向 Postgres

- **Status**: Proposed(Zewang 倾向 PG;原设计者倾向 Redis,待 2026-08-24 晚同步,
  详见 docs/sync-agenda-2026-08-24.md 附录)
- **Date**: 2026-08-24

## Context

Checkpointer 承载:多轮会话状态、interrupt 挂起的写确认载荷、(若 B 留在 LangGraph)
批处理断点续跑进度。选型在 langgraph-checkpoint-redis 与 langgraph-checkpoint-postgres
之间。关键约束:ADR-0005 定「写路径 fail-closed」,checkpointer 是写路径硬依赖。

## Decision(倾向)

**Postgres**,三条理由按分量排:

1. **延迟优势在本负载归零**:checkpoint 写入发生在每个 super-step 末,而每步内含
   LLM 调用(0.5~5s)。Redis ~0.5ms vs PG ~3ms 的差 <1% 步耗时,用户不可感知。
   Redis 的延迟优势只在纯状态高吞吐场景兑现。
2. **持久性是分水岭**:Redis 默认内存态重启全丢;AOF everysec 仍有 ≤1s 丢失窗口;
   内存压力下 noeviction=写报错,allkeys-lru=**逐出挂起 interrupt(确认静默丢失)**。
   丢挂起确认是生产事故级风险,与 fail-closed 哲学相悖。PG WAL+fsync 提交即持久。
3. **运维账**:Postgres 已在部署图(Langfuse 自带 PG;M4 pgvector)。checkpointer
   用 PG 则 Redis 可从 agent 栈整体退场(v1 限流/预算=进程内),有状态服务 2→1,
   备份=标准 pg_dump/PITR;thread 状态可 SQL 运维查询(找挂起确认/僵尸 thread)。

## Redis 的真实论点与回应

- **原生 TTL**:真实优势,但 PG 侧一条清理 job(`DELETE ... WHERE updated_at < ...`
  挂 cron)可替代,换来不被逐出的确定性。
- **「聊天热数据放热存储」直觉**:在 LLM 步耗面前不成立(见理由 1)。

## 若最终选 Redis 的底线三件套

AOF 开启 + maxmemory-policy=noeviction + 内存容量规划(会话数×平均 checkpoint 大小)。
三者缺一即挂起确认有静默丢失路径,须回写 ADR-0005 风险清单。

## Consequences

- 选 PG:需补清理 job(thread TTL 策略);B 批处理断点续跑直接受益;Redis 退栈待
  限流/预算方案确认后执行。
- 本 ADR 在同步后按结论改 Status(Accepted / superseded)。
