# ADR-0005: 写操作确认令牌与失败哲学

- **Status**: Accepted
- **Date**: 2026-08-24

## Context

写工具(submit_resume_score / handle_reschedule;move_interview 已确认砍除——对应
后端端点已 deprecated)的 confirmation_token 目前只查非空(write.py
`_require_token` 的 TODO 自认未校验真实性),模型自编字符串即可绕过。需要定
完整机制。「生产级」同时要求明确系统级失败的降级原则。

## Decision

### 确认令牌契约(对话确认)

- **唯一发行方**:LangGraph interrupt 恢复路径。流程:图到写工具 → interrupt
  挂起(携带操作摘要)→ 用户在聊天里批准/拒绝 → `Command(resume=...)` 恢复,
  恢复值即令牌。
- **绑定**:令牌对应当前 thread 挂起记录的操作指纹(hash(工具名+关键参数));
  工具校验「令牌对应的操作 == 本次调用」,不一致即拒。
- **一次性**:执行即作废;不落库、不跨 thread、不可重放。
- **拒绝也是恢复**:用户拒绝 → 同样走恢复流程,拒绝结果作为工具结果回喂轨迹,
  模型重新规划(不是异常路径)。
- v2 加固方向(非本期):expected_* checklist 参数——模型调用前自报期望前置
  状态,工具/服务端比对,不一致即拒绝并告警(服务端真值校验,不采信模型自报)。

### 失败哲学

- **工具/后端错** → 映射为可行动文案(哪个操作、为何失败、用户能做什么),
  模型只见映射文案不见堆栈(TOOL-06)。
- **模型错**(超时/限流)→ 指数退避重试 ≤3 次,仍败回 canned 道歉 + trace_id。
- **系统错**分两类,总原则:**观测 fail-open,写路径 fail-closed**——
  Langfuse 挂了聊天照常(只丢 trace);checkpointer 不可用 = 无法挂起确认 =
  拒绝一切写操作。宁可不可用,不可不可信。

## Consequences

- write.py 的 `_require_token` 从「非空检查」升级为「指纹校验」,TOOL-04
  实现以此为准。
- B 流水线批量写回走评审确认语义(见 CONTEXT.md),不经对话令牌——两套机制
  不可互替。
- fail-closed 意味着 Redis(checkpointer)是写路径硬依赖:其持久化(AOF)与
  TTL 策略(≥ 招新周期 + N 天)成为上线前检查项。
