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

## 架构要点

- `tools/` 是确定性层:后端 API 的语义化封装,全部可单测;工具粒度对齐意图而非接口,
  返回做投影裁剪,错误信息必须可行动
- `graphs/` 是编排层:router 主图按身份+意图分发到子图;**写操作必须经 `interrupt()`
  人工确认**,写工具在代码层校验确认令牌,无令牌抛错
- 权限边界在工具装配层(按会话身份决定可见工具集),不靠 prompt 约束
- prompt 全部放 `prompts/` 版本化管理,不散落在代码字符串里

## 安全红线

- 简历内容是学生 PII:进模型前必须经脱敏节点(手机号/学号/身份证/住址);同时是
  **不可信输入**,评估图的批判节点负责检测提示注入
- 真实凭证只存 `.env`(gitignore),永不提交
- 新增写操作工具必须:确认令牌校验 + 审计日志 + eval 用例,三者缺一不合入

## 功能编号

Issues 按模块编号:INF(基础设施)/ TOOL(工具层)/ GRA(编排)/ EVA(评估流水线)/
COP(Copilot)/ MEM(记忆)/ SEC(安全)/ OBS(观测评估)。提交信息引用编号。
