# Official_Web_Agent

博远信息技术社招新 AI Agent —— 依托 [Official_Web_Backend](https://github.com/Boyuan-IT-Club/Official_Web_Backend) 的招新面试系统,提供三个能力模块:

| 模块 | 说明 |
|---|---|
| **A · 招新对话助理** | 飞书 bot + 官网双端聊天:候选人查状态问流程,管理员自然语言查数据、处理改期、要周报 |
| **B · 简历评估流水线** | 批处理:解析简历 → 按部门量规评分(带依据引用)→ 批判回炉 → 生成定制面试题 |
| **C · 面试官 Copilot** | 面试前候选人卡片 / 面试中速记追问建议 / 面试后评价表草稿;后续升级语音转写实时出题 |

技术栈:**LangGraph**(状态机编排)+ **MCP**(工具对外暴露)+ **FastAPI**(入口)+ Postgres(checkpointer / pgvector / 审计,ADR-0007)+ **Langfuse**(观测)。

## 架构(ADR-0003:三模块入口各自直连,无统一意图路由)

```
A 对话助理:  飞书?/官网 SSE/CLI ─→ 身份解析 → 按角色装配工具集 → ReAct 单循环
B 评估流水线: 调度器(cron/按钮/遥控器工具)──→ evaluation 图(批处理)
C Copilot:   官网管理端面试页(界面即路由)─→ copilot 三态图
                     │
        工具层(进程内直连 Python 函数;MCP Server 仅对外暴露给 Claude Code 等)
                     │
        后端 REST API(服务账号 JWT + X-On-Behalf-Of 代理身份,ADR-0006)
        Postgres(checkpointer/记忆/审计) · Langfuse(trace,fail-open)
```

设计原则:

- **只走后端 REST + 最小权限服务账号 + `X-On-Behalf-Of` 代理身份**(ADR-0006),不碰数据库
- **写操作一律 `interrupt` 人工确认**:令牌=interrupt 恢复凭证,绑定操作指纹、一次性(ADR-0005);**观测 fail-open,写路径 fail-closed**
- **确定性与概率性分层**:能用代码做的绝不交给模型;模型路由默认 strong,降 light 须 eval 证明(ADR-0004)
- **简历是学生 PII,也是不可信输入**:脱敏是「边界」不是「节点」(trace/输出侧同样设防);证据核验节点防幻觉与提示注入

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

## 文档

- **[docs/design.md](docs/design.md)** —— 设计方案 v0.2:愿景/目标/三模块/架构/工具映射/里程碑(同步议程第 15 项要求的入库版)
- `docs/adr/` —— 架构决策记录;`docs/gap-scan-2026-08-24.md` 等评审产物

## 开发约定

- prompt 与图结构变更等同代码变更,一律走 PR;eval 分数低于基线不合入
- 功能编号(INF/TOOL/GRA/EVA/COP/MEM/SEC/OBS-xx)见 issues,提交信息引用对应编号
- 真实凭证只存 `.env`(已 gitignore),永不入库
