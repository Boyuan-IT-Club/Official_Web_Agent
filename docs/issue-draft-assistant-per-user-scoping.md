# [提] 问答助手按提问者本人权限收敛工具与数据 —— 修复服务账号代读越权(关联 SEC-02 #53)

## 描述

官网客服问答助手(web/M6,只读)当前装配查询工具按「粗粒度 role 三档」硬编码
(`assistant/_ROLE_TOOL_NAMES`),而查询工具执行经**进程内共享的服务账号单例**
(`readonly.get_backend_client()`),不是来问者本人的 JWT。

后端 V22 已把 `resume:view` / `evaluation:view` 等从**社员(MEMBER)角色撤下**
(V22__tighten_dept_and_member_scope.sql、ADR-0003 / 0006)。但 agent 装配层未同步:
member 档仍授予 `search_resumes` / `get_resume_detail` / 场次等工具,且它们经
服务账号(被授 admin 级只读)执行 —— 成员经问答助手可读到后端已 403 他的跨界简历
(PII)。形成与后端 RBAC 不同步的**服务账号代读越权面**。

## 期望

问答助手从来访者本人官网 JWT 判断可用工具与可见数据,后端 `@PreAuthorize` 独立复核
(两层防线,ADR-0006)。任何用户只能读到其本人权限码对应的数据,不借机器身份代读。

## 现方案(装配 + 调用两层)

- 每个只读工具声明其最低门槛权限码(per-tool `required_permissions`)。
- `assemble_tools(identity)` 遍历时仅注入 `required ⊆ identity.permission_codes`
  的工具(替换 bottom `_ROLE_TOOL_NAMES` role 三档)。
- 数据请求一律以来问者本人 JWT 经 `get_as_user` 裸发(对齐 `get_my_interview` 先例),
  **废除这些查询工具的服务账号直调**;后端复核收口。

### per-tool 门槛码(已逐条从后端注解核对)

| 工具 | 后端端点 | 门槛权限码 |
|---|---|---|
| `search_resumes` | `/api/resumes/search` | `resume:view` |
| `get_resume_detail` | `/api/resumes/admin/{userId}/{cycleId}` | `resume:view` |
| `find_available_sessions` | `/api/interview/admin/cycles/{id}/available-sessions` | `interview:schedule` 或 `resume:audit` |
| `list_unassigned` | `/api/interview/admin/cycles/{id}/unassigned` | `interview:schedule` 或 `resume:audit` |
| `list_reschedule_requests` | `/api/interview/reschedule/admin/list` | `interview:schedule` 或 `resume:audit` |
| `get_recruit_statistics` | 聚合 `result/list` + `summary` + `resumes/search` | 混合(`resume:view`/`interview:result`/`board:manage`) |
| `get_my_interview` | `/api/interview/schedule/my` | `isAuthenticated`(自 scope) |
| `get_open_cycle` | `/api/cycles/open` | 开放(登录即可) |
| `search_knowledge` | KB(RAG #134) | public 工具面 |

## 影响 / 行为变化

- candidate:不变(已只 read self,越权不可达)。
- member:不再能检索/读他人简历;tool 面收窄到其真实持有权限码对应的只读。
- admin:语义不变(持 resume:view + 相关),但请求归因从服务账号改为本人,审计可追溯。
- 服务账号 `svc-agent` 退居**纯批处理**(评估流水线 cron)身份,不再被在线问答使用。

## 关联

- 依赖/重叠:SEC-02 #53「按身份装配工具集」——本 issue 为其在问答助手 web 通道的
  **权限码级落地 + 越权堵口**,若 #53 已规划同类改造请并入同实现。
- ADR-0006「社团官网层问答助手」(2026-09-09 修订)已记录该原则。
- 只读决断:问答助手不装配写工具(ADR-0006 / assistant 模块头注 2026-09-09),本 issue
  不涉及写路径。

## 验收

1. member 登录问答助手问「技术部有哪些简历」→ 工具不装配/后端 403,不返回他人 PII。
2. 用户数据请求带本人 JWT(请求级审计回到本人),非服务账号。
3. `svc-agent` 仅出现在批处理(pipeline)调用,在线 web 通道归零。
4. per-tool 门槛码落库/落常量并有装配层单测。
