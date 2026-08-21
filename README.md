# Official_Web_Agent

博远信息技术社招新 AI Agent —— 依托 [Official_Web_Backend](https://github.com/Boyuan-IT-Club/Official_Web_Backend) 的招新面试系统,提供三个能力模块:

| 模块 | 说明 |
|---|---|
| **A · 招新对话助理** | 飞书 bot + 官网双端聊天:候选人查状态问流程,管理员自然语言查数据、处理改期、要周报 |
| **B · 简历评估流水线** | 批处理:解析简历 → 按部门量规评分(带依据引用)→ 批判回炉 → 生成定制面试题 |
| **C · 面试官 Copilot** | 面试前候选人卡片 / 面试中速记追问建议 / 面试后评价表草稿;后续升级语音转写实时出题 |

技术栈:**LangGraph**(状态机编排)+ **MCP**(工具层)+ **FastAPI**(入口)+ Redis / Postgres+pgvector(记忆)+ **Langfuse**(观测)。

## 架构

```
飞书事件/卡片 ─┐
官网 SSE 聊天 ─┼─→ Router 主图 ─→ 业务子图(助理/评估/Copilot)
CLI(调试)  ─┘        │                    │
                 记忆(Redis+Store)    MCP 工具层 ─→ 后端 REST API(服务账号 JWT)
                       │
                 Langfuse(trace/评估)
```

设计原则:

- **只走后端 REST + 最小权限服务账号**,不碰数据库,后端零侵入
- **写操作一律 `interrupt` 人工确认**,权限边界在工具层代码里,不靠 prompt
- **确定性与概率性分层**:能用代码做的绝不交给模型
- **简历是学生 PII,也是不可信输入**:进模型前脱敏,批判节点防提示注入

## 目录结构

```
src/boyuan_agent/
├── config.py       # pydantic-settings 配置
├── cli.py          # CLI 对话入口(开发调试)
├── mcp_server.py   # MCP Server:工具对外暴露(Claude Code 可直接挂载)
├── tools/          # 后端 API 语义化封装(确定性,可单测)
├── graphs/         # LangGraph 图:router + assistant/ + evaluation/ + copilot/
├── memory/         # checkpointer 与长期记忆 Store
├── prompts/        # 版本化 prompt
└── feishu/         # 事件订阅、卡片、多维表格
evals/              # 评估数据集与断言,CI 门禁
tests/              # 确定性单测
```

## 快速开始

```bash
uv sync
cp .env.example .env   # 填入后端地址、服务账号、模型 API key
uv run boyuan-agent chat --role admin   # CLI 对话(M1)
uv run python -m boyuan_agent.mcp_server  # 启动 MCP Server(stdio)
```

## 开发约定

- prompt 与图结构变更等同代码变更,一律走 PR;eval 分数低于基线不合入
- 功能编号(INF/TOOL/GRA/EVA/COP/MEM/SEC/OBS-xx)见 issues,提交信息引用对应编号
- 真实凭证只存 `.env`(已 gitignore),永不入库
