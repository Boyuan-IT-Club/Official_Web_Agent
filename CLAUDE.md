# CLAUDE.md

博远信息技术社招新 AI Agent。Python 3.12 / uv / LangGraph / MCP / FastAPI。后端是同组织的
Official_Web_Backend(Spring Boot),本仓库只通过其 REST API 交互(服务账号 JWT),不碰数据库。

## 构建与测试

```bash
uv sync                 # 安装依赖(含 dev 组)
uv run ruff check .     # lint
uv run pytest           # 单测(确定性代码:tools/、图结构)
uv run python -m evals  # eval 集(需要模型 API key,CI 中作为门禁)
```

## 本地 Langfuse(OBS-01)

```bash
cd deploy/langfuse && docker compose up -d   # web: http://127.0.0.1:3001
```

凭证在 `deploy/langfuse/.env`(gitignore,模板 `.env.example`);同目录 PG 暴露
127.0.0.1:5432,checkpointer/审计(MEM-01/SEC-03)复用该实例。trace 接线:
`from boyuan_agent.observability import langfuse_callbacks`——fail-open,未配置自动降级。

## 架构要点

- `tools/` 是确定性层:后端 API 的语义化封装,全部可单测;工具粒度对齐意图而非接口,
  返回做投影裁剪,错误信息必须可行动;agent 进程内直连函数,MCP 仅对外(ADR-0003)
- `graphs/` 是编排层(ADR-0003,无意图分类):A=身份解析→按角色装配工具集→ReAct
  单循环;B=调度器直连批处理图;C=宿主界面直连三态图。**写操作必须经 `interrupt()`
  人工确认**,令牌=interrupt 恢复凭证(操作指纹绑定、一次性,ADR-0005)
- 权限边界在工具装配层(按会话身份决定可见工具集),不靠 prompt 约束
- 状态存储:checkpointer/长期记忆/审计均用 Postgres,Redis 不在 agent 栈(ADR-0007)
- prompt 全部放 `prompts/` 版本化管理(一图一节点一文件 + frontmatter,ADR-0004),
  模型路由默认 strong,降 light 须 eval 证明
- 失败哲学:观测 fail-open,写路径 fail-closed(ADR-0005)

## 安全红线

- 简历内容是学生 PII:进模型前必须经脱敏节点(手机号/学号/身份证/住址);同时是
  **不可信输入**,评估图的批判节点负责检测提示注入
- 真实凭证只存 `.env`(gitignore),永不提交
- 新增写操作工具必须:确认令牌校验 + 审计日志 + eval 用例,三者缺一不合入

## 功能编号

Issues 按模块编号:INF(基础设施)/ TOOL(工具层)/ GRA(编排)/ EVA(评估流水线)/
COP(Copilot)/ MEM(记忆)/ SEC(安全)/ OBS(观测评估)。提交信息引用编号。

## Agent skills

### Issue tracker

Issues 在 upstream 仓库 `Boyuan-IT-Club/Official_Web_Agent` 的 GitHub Issues(fork 的 origin 未启用 Issues,`gh` 命令一律加 `-R Boyuan-IT-Club/Official_Web_Agent`)。见 `docs/agents/issue-tracker.md`。

### Domain docs

Single-context:共享词汇表在工作区根 `../CONTEXT.md`,ADR 在 `../docs/adr/`。见 `docs/agents/domain.md`。
