# ADR-0006: 代理身份与审计契约——每笔 agent 行为可归因、可回溯

- **Status**: Accepted(2026-09-01 重写:per-user PAT 模型取代 X-On-Behalf-Of;
  后端增量执行项见 docs/sec-01-negotiation.md v2)
- **Date**: 2026-08-24 初版 / 2026-09-01 修订
- **Supersedes**: 本文档 2026-08-24 版的「X-On-Behalf-Of 请求头」方案

## Context

后端授权只认 Bearer JWT 的 permissionCodes。agent 全程持单一服务账号 JWT
调后端 → 后端视角所有请求都是服务账号:权限判定与审计均丢失最终用户归因,
工具装配层成为唯一防线(单防线)。

项目所有者要求(2026-08-24):所有经 agent 执行的行为必须可审计、可回溯,
审计不止记录用户身份,还要记录**是哪个 agent**(通道/模块/图)执行的。
追加要求(2026-09-01):**每个用户能自管理自己 agent 的权限范围**——
有人希望 agent 权限多、有人希望少;后端管理面要能辨识请求的**来源通道**。

参考系:飞书 Lark CLI 的「单应用 + 双身份(user/tenant token) + 双层 scope
收敛(应用声明 ∩ 用户授权)」模型(调研 2026-09-01,来源见 sec-01 文档)。

## Decision

### 身份三通道(谁的名义干活)

| 通道 | 身份 | 权限 | 来源标识 |
|---|---|---|---|
| MCP 挂载(Claude Code 等) | **PAT**:用户签发给 agent 的受限 token | 用户勾选的权限码子集 ∩ agent 工具白名单 | `X-Agent-Channel: mcp` |
| 官网对话(浮窗/管理面板) | 官网会话 JWT(随请求,agent 不存储) | 用户全量(用户在线 + interrupt 确认闸) | `X-Agent-Channel: web` |
| 批处理(评估流水线 cron) | `svc-agent` 服务账号(全局机器身份) | 只读 + 评估写回,最小集 | `X-Agent-Channel: pipeline` |

CLI 调试入口与 MCP 同为 PAT(`X-Agent-Channel: cli`)。

### PAT(Per-user Agent Token)契约

- **语义**:用户**主动签发**给 agent、令其代表自己干活的长期受限凭证——
  token 带用户身份 + 用户勾选的权限码子集(复用现有 RBAC permissionCodes,
  不发明新粒度)。归因天然正确:token 即「替张三干活」,取代原方案的
  X-On-Behalf-Of 头。
- **scope 双层收敛**:生效权限 = PAT 携带的权限码(用户勾选,只能缩不能扩)
  ∩ agent 装配层工具白名单(SEC-02)。用户勾选界面展示工具名,落库映射权限码。
- **生命周期**:7 天有效 + 官网设置页一键续期(重签);**吊销列表**为安全
  底线——后端记 revoked token,官网可随时吊销(比续期更重要)。
- **通道头**:`X-Agent-Channel` 每请求必带,自报家门(非安全边界,安全由
  token 判定);后端日志/管理面据此辨识来源(同一用户可能同时用 MCP 与官网)。

### 服务账号(svc-agent)契约

仅批处理场景使用(无人在线,没有「替谁」)。权限=只读+评估写回最小集;
归因靠**记录触发者**(管理员点「开始审核」时的 acting_user),不经代理头。

### `GET /api/auth/me`

保留:官网通道身份解析(GRA-01)——agent 凭会话 JWT 换用户资料与角色,
不接触 JWT_SECRET。agent 永不持有 JWT_SECRET(不变)。

### 审计与回溯契约(SEC-03 / OBS-02)

- **权威存储**:agent 侧 Postgres(与 Langfuse 同实例;Langfuse 可删改,
  只作执行细节视图,不作权威)。
- **审计行**(写操作必备)字段:
  - `acting_user_id` —— PAT/会话 JWT 的用户,或批处理触发者
  - `channel` —— mcp | web | cli | pipeline(与 X-Agent-Channel 一致)
  - `agent` —— 模块/图/节点 + prompt 版本(是哪个 agent 干的)
  - `action` —— 工具 + 参数 + 操作指纹(与确认令牌同指纹,ADR-0005)
  - `decision` —— 谁批准/拒绝(interrupt 恢复记录)
  - `result` / `trace_id` / `timestamp` —— trace_id 串 Langfuse 全过程
- 写路径三重闸不变:工具装配(读不到)→ interrupt(执行不了)→ 指纹令牌
  (绕不过);PAT 的 scope 是第四道(后端侧判定)。

## Consequences

- 后端增量变化(见 sec-01-negotiation v2):删除 X-On-Behalf-Of 项;
  新增 PAT 签发/列表/吊销端点(官网设置页);服务账号缩到批处理口径。
- 防线从一层(装配)变两层(装配 + PAT scope/后端判定);在线场景的归因
  不再依赖额外协议头,凭证本身即归因。
- PAT 7 天 + 续期是简单起步:不做 refresh_token 滚动状态机(飞书式 UAT 2h
  + refresh 7d + 365d 重授权),真有多端长期授权需求再升级。
- 官网通道 agent 不存 token(随请求来去),token 保管面只剩 PAT(7 天短命,
  泄漏窗口有限)。
