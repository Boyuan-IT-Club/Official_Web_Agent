# SEC-01 后端增量:核实版决策清单

> 目的:agent(Boyuan-IT-Club/Official_Web_Agent)上线前需要后端
> (Official_Web_Backend)的一组增量。本文每项已按后端代码逐条核实
> (2026-09-01),给出实现挂载点与真实改动量,供后端负责人逐项拍板。
> 状态标记:【待拍板】【已核实待做】【建议放弃】。
> 关联:ADR-0006 · issue #52 · #69

## 背景一句话

agent 全程持服务账号 JWT 调后端,后端只认 JWT 里的 permissionCodes——
权限判定与审计都丢失「最终用户是谁」;另有评估流水线的评分数据无处落库。

---

## 第 1 项 最小权限服务账号【已核实,纯 SQL,零 Java】

- **代码事实**:RBAC 五表齐备(`V6__core_schema_baseline.sql:77-130`:
  user/role/permission/user_role/role_permission);user 表有
  status(0禁用/1启用)列(:61),无需新增机器账号标记列。
- **做法**:一个 Flyway 迁移(V33+)——INSERT `svc-agent` 用户行 + 新建
  `AGENT_SERVICE` 角色 + 按现有 permission_code(非硬编码 id!)关联最小权限集。
- **⚠ 必须遵守的既有教训**(权限种子事故,V13→V18 复盘):种子禁止
  「硬编码 permission_id + INSERT IGNORE」——线上 id 已被手工数据占用时会静默
  跳过。按 `permission_code` 存在性插入(V18 模式)。
- **权限集建议**:user:view + resume:view + 简历检索相关只读 + 面试管理端点
  所需码(与 agent 工具面对照后定稿)。

## 第 2 项 X-On-Behalf-Of 代理头【已核实,中等工作量】

- **代码事实**:
  - 过滤链顺序(SecurityConfig.java:88-90):serviceTokenFilter → jwtFilter
    → UsernamePasswordAuthenticationFilter。**挂载点明确:jwtFilter 之后**。
  - JWT claims 已含 userId/roleNames/permissionCodes(JwtTokenUtil.java:104),
    服务账号 JWT 先正常认证,再读代理头。
  - 下游判定点已定位:`EvaluationBoardServiceImpl.getCandidateResume`
    (:154-164)的场次绑定校验 `isInterviewerOf(sessionId, viewerUserId)`——
    2005 的来源,viewerUserId 正是代理头要替换的输入。
- **做法**:新薄 Filter(~60 行,参照 ServiceTokenAuthenticationFilter 的
  OncePerRequestFilter 模式)——仅当 principal 为服务账号时读
  `X-On-Behalf-Of: <user_id>` 装入 request attribute/SecurityContext details;
  下游 4~5 个取 viewerUserId 的调用点改为「有代理头用代理头」。
- **安全边界(必须)**:普通用户 JWT 携带此头一律忽略——防用户伪造他人代理;
  服务账号 + 代理头 = 双重身份,服务账号裸调(无头)维持现有行为(2005)。

## 第 3 项 GET /api/auth/me【已核实,小工作量】

- **代码事实**:AuthController(6 个端点:register/login/logout/发码×2/
  reset-password)无 /me;claims 已有 userId/roleNames/permissionCodes,
  身份部分零查库,资料(name/email/avatar)一次 selectById。
- **做法**:一个端点 + 一个 DTO(~40 行),从 SecurityContext 取当前用户。
- **战略意义**:agent 验官网 token 不接触 JWT_SECRET(HS256 共享密钥,
  持有即具备签发能力)——官网入口身份解析(GRA-01)的唯一非红线路径。

## 第 4 项 审计权威存储【待拍板,倾向 b,后端零改动】

- **代码事实**:后端无任何审计表/审计端点(grep 全库无 audit 命中)。
- **三选一**:a) 后端建审计表+内部端点;b) **agent 侧 Postgres(倾向)**——
  agent 行为的审计跟着 agent 走,badcase 回流(OBS-06)同库串联 trace_id;
  c) 文件+聚合。
- 选 b 后端零改动,只需在本文确认。

## 第 5 项 评估草稿/评审端点【已核实,大,单独排期】

- **代码事实**:`PUT /api/resumes/{resumeId}/score`(ResumeController:312-319)
  只收 int,注释自述「resume_score 列此前只有飞书导出在读」;已带打分人署名
  (`updateResumeScore(..., currentUser().getUserId())`)——署名链路可复用。
- **需要**:草稿表(resume_id/cycle_id/维度分+依据 JSON/量规版本/prompt 版本/
  状态/评审人)+ 暂存/列表/确认落库 3~4 端点。schema 草案由 agent 侧 #69 出。
- **不阻塞**第 1~3 项;评估结果过渡期可落 agent 侧 PG + 飞书多维表格。

## 第 6 项 面试统计端点【建议放弃】

- **代码事实**:全库 Java 无 statistics 实现(仅 openapi 里 deprecated 标注)。
- agent 侧聚合工具(PR #73)已真链路验证可用——后端不做。

---

## 拍板与执行顺序

| 项 | 状态 | 后端工作量 | 建议顺序 |
|---|---|---|---|
| 1 服务账号 | 已核实待做 | 一个 SQL 迁移 | **第一批**(agent 生产前提) |
| 3 /api/auth/me | 已核实待做 | ~40 行 | **第一批**(GRA-01 前置) |
| 2 X-On-Behalf-Of | 已核实待做 | Filter+5 调用点 | 第二批(Copilot 卡片激活) |
| 4 审计存储 | 待拍板(倾向 b) | 0 | 随第一批口头确认 |
| 5 评估草稿端点 | 已核实待做 | 大 | M2 前单独排期 |
| 6 统计端点 | 建议放弃 | — | — |

第一批合计:一个迁移 + 一个端点,后端侧半天内;谈成即解除 agent 上线
与官网入口两个阻塞。
