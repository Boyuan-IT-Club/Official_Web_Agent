# ADR-0006: 代理身份与审计契约——每笔 agent 行为可归因、可回溯

- **Status**: Proposed(agent 侧契约已定;后端增量待 SEC-01 与后端负责人谈判确认)
- **Date**: 2026-08-24

## Context

后端授权只认 bearer JWT 的 permissionCodes,且无任何 introspection 端点
(openapi 的 /api/auth/ 下仅 register/login/reset-password/发码/logout)。agent
全程持单一服务账号 JWT 调后端 → 后端视角所有请求都是服务账号:权限判定与审计
均丢失最终用户归因,工具装配层成为唯一防线(单防线)。

项目要求(2026-08-24 所有者确认):所有经 agent 执行的行为必须可审计、可回溯;
且审计不止记录用户身份,还要记录是**哪个 agent**(模块/图)执行的。

## Decision

### SEC-01 后端增量三件套(一次 PR 谈判)

1. **最小权限服务账号**(agent 专用)。
2. **`X-On-Behalf-Of: <user_id>` 请求头**:后端按最终用户身份判权限 + 记审计;
   防线从一层(工具装配)变两层(装配 + 后端判定)。
3. **`GET /api/auth/me`**:JWT → 用户 + 角色。一石二鸟——agent 验官网 token 无需
   接触 JWT_SECRET(现为 HS256 共享密钥,持有即具备签发能力);同时就是 GRA-01
   官网入口身份解析的实现。RS256/JWKS 缓到 v2。

退路(若后端拒绝增量):agent 进程内单防线——每个管理工具执行前二次校验会话
role,审计仍记录 acting user,但在本文档明写「单防线、风险自负」。

### 审计与回溯契约(SEC-03 / OBS-02)

- **trace 全量**:一切工具调用(读 + 写)进 Langfuse,trace_id 全链路透传。
- **审计行(写操作必备)**,字段:
  - `acting_user_id` —— X-On-Behalf-Of 的最终用户(是谁授意)
  - `agent` —— 哪个模块/图执行(assistant / evaluation / copilot),含节点与
    prompt 版本(是哪个 agent 做的)
  - `action` —— 工具 + 参数 + 操作指纹(与确认令牌绑定同一指纹,ADR-0005)
  - `decision` —— 谁批准/拒绝(对话确认的恢复记录)
  - `result` / `trace_id` / `timestamp`
- badcase 回流(OBS-06)以 trace_id 串联审计行与 Langfuse trace。

## Consequences

- 后端审计从「服务账号做了 X」变为「用户 U 经 agent-A 的节点 N 做了 X,由 U 确认」。
- `GET /api/auth/me` 落地前,GRA-01 的官网入口身份解析没有实现路径——SEC-01
  仍是 TOOL-01 的前置(与既有路线图一致)。
- agent 侧永不持有 JWT_SECRET。
