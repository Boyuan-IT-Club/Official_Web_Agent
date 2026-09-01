# SEC-01 后端增量:决策清单 v2(共识版)

> 2026-09-01 与后端负责人(兼项目所有者)grill 达成共识:per-user PAT 模型
> 取代 X-On-Behalf-Of。本版是执行清单;身份模型细节见 ADR-0006(Accepted)。
> 关联:ADR-0006 · issue #52 · #69

## 模型一句话

三种身份按通道分派:MCP=PAT(用户签发给 agent 的 7 天受限 token)/
官网=会话 JWT(不存储)/批处理=svc-agent 机器账号;每个请求带
`X-Agent-Channel` 头标识来源;审计权威在 agent 侧 Postgres。

---

## 执行项

### 1. svc-agent 服务账号【缩水保留,纯 SQL】

- 仅批处理用;权限=只读+评估写回最小集。
- Flyway 迁移(V33+):user 行 + `AGENT_SERVICE` 角色 + 按 permission_code
  判重关联(V18 模式,严禁硬编码 id + INSERT IGNORE——V13 事故教训)。

### 2. PAT 签发/管理(新增,替换原 X-On-Behalf-Of 项)【主要工作量】

- 三个端点 + 官网设置页:
  - `POST /api/auth/agent-tokens`——签发:当前用户勾选权限码子集,
    返回 7 天 token(scope 只能是本人权限的子集,只能缩不能扩)
  - `GET /api/auth/agent-tokens`——列表(我发过哪些、何时到期)
  - `DELETE /api/auth/agent-tokens/{id}`——吊销(安全底线)
- token 内含:userId + scope(权限码子集)+ 过期 + token id(供吊销比对)。
- 认证链:现有 JwtAuthenticationFilter 识别 PAT 类型 claims,权限判定
  按 scope 子集(而非用户全量)。
- 官网设置页:勾选展示 agent 工具名,落库映射权限码。

### 3. `GET /api/auth/me`【保留,小】

- ~40 行;官网通道身份解析(GRA-01),agent 验会话 token 不碰 JWT_SECRET。

### 4. 审计权威存储【已拍板:agent 侧 Postgres,后端零改动】

- agent 行为审计跟着 agent 走;字段契约见 ADR-0006(含 channel)。

### 5. 评估草稿/评审端点【保留,大,M2 前单独排期】

- schema 草案由 agent 侧 #69 出;score 署名链路可复用。

### 6. `X-Agent-Channel` 请求头【后端零改动起步】

- agent 每请求自带(mcp/web/cli/pipeline);非安全边界,后端 access log
  先收着,管理界面需要时再消费。

### 7. 面试统计端点【维持放弃】

---

## 已删除项

- **X-On-Behalf-Of 头**(v1 第 2 项):PAT 自带用户身份,在线场景不再需要
  代理头;批处理归因用触发者记录。v1 的「过滤器+5 调用点改造」整体取消。

## 执行顺序

| 批次 | 内容 | 后端工作量 |
|---|---|---|
| 第一批(agent 生产前提) | 1 服务账号 + 3 /me | 一个迁移 + ~40 行 |
| 第二批(MCP 长期使用) | 2 PAT 三端点 + 设置页 | 中等,含前端配合 |
| 第三批(M2 前) | 5 评估草稿端点 | 大,单独排期 |

参考系:飞书 Lark CLI 双身份+双层 scope 模型调研
(github.com/larksuite/cli · github.com/larksuite/lark-openapi-mcp ·
open.feishu.cn user_access_token 文档;2026-09-01)。
