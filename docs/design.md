# 设计方案

> v0.3 · 2026-09(结构化重构后)。v0.1(2026-08-21)初始设计,v0.2(2026-08-25)并入
> 架构评审决议;本版更新为**当前形态**:补代码导览与关键流程,里程碑规划(M1~M6)与
> 谈判清单等过程内容移入 `docs/archive/` 与 ADR。与 ADR 冲突时以 ADR 为准;工具↔端点
> 映射以后端仓库 `openapi.yaml` 为唯一真实来源。

## 1. 愿景与目标

**愿景**:把招新季"人肉串流程"的部分交给 agent——候选人有问题不用等管理员翻后台,
简历初筛不再是纯人工逐份过,面试官不用一边提问一边低头填表。同时,本仓库是一次完整的
agent 工程实践:ReAct 工具调用、状态机编排、human-in-the-loop、记忆、评估飞轮,
都要在真实业务上落地。

**目标**

- 落地三个能力模块(A 对话助理 / B 评估流水线 / C 面试官 Copilot),共用一套工具层与
  状态设施,而非三个孤立脚本
- 与后端**完全解耦**:只通过 REST API + 最小权限服务账号 + `X-On-Behalf-Of` 代理身份
  (ADR-0006)交互,不触碰后端数据库
- 写操作一律经 LangGraph `interrupt` 人工确认,令牌=interrupt 恢复凭证(操作指纹绑定、
  一次性,ADR-0005);agent 只加速流程、不替人拍板
- 从第一个 PR 起就有 trace(Langfuse)和 eval 集,质量可回归、变更可门禁
- 每笔 agent 行为可审计、可回溯:归因到最终用户 + 执行模块/节点/prompt 版本(ADR-0006)

**非目标**

- **不做自动录取决策**——评估流水线输出是"辅助初筛参考",最终决定永远由人做
- 不改造后端核心业务逻辑;不接管排期分配算法
- 第一期候选人侧只读
- 不用 LangChain 经典 Chain/AgentExecutor;编排全部走 LangGraph,langchain-core 只作
  model/tool 接口层

## 2. 三个能力模块

三者是一条价值链:**B 产出的候选人摘要与定制面试题,在 C 中被面试官消费;C 产出的评价
回流后端评价看板;A 是管理员触发 B、查看进度与数据的对话入口。**

| 模块 | 入口(ADR-0003:各入口直连,无统一意图路由) | 形态 |
|---|---|---|
| **A · 招新对话助理** | 官网 SSE(`/api/agent/chat`,已上线);CLI(`official-agent` 命令,开发调试);飞书(规划,未接入) | 身份解析 → 按角色装配工具集 → ReAct 单循环;写操作经 interrupt 指纹令牌 |
| **B · 简历评估流水线** | 管理面 `/admin/evaluation`(触发/重试/评审);启动自动恢复 | job 状态机 → 评分子图(评分+批判回炉)→ 出题 bundle → 落库/评审队列 |
| **C · 面试官 Copilot** | 官网管理端面试页(规划中) | 卡片 / 速记追问建议 / 评价草稿三态 |

## 3. 总体架构

```
A:官网 SSE(web/routes)· CLI ─┐
B:管理面(admin 面)· 启动恢复 ─┼→ graphs/(LangGraph 编排)
C:管理端面试页(规划)      ─┘        │
                                tools/(后端 REST 语义化封装,进程内直连;
                                MCP Server 仅对外暴露,维护态)
                                    │
                    state/(Postgres 状态面)· kb/(pgvector 知识库)
                                    │
    后端 REST(服务账号 JWT + X-On-Behalf-Of 代理身份)
    Postgres(checkpointer / 状态表 / pgvector / 审计) · Langfuse(trace,fail-open)
```

关键决策(详见对应 ADR):

- **入口各自直连,无统一意图路由**(ADR-0003)
- **工具进程内直连**;MCP 纯对外给 Claude Code 等挂载(ADR-0003),不作用户入口
- **上下文四段组装 + 双 cache 断点**;超阈值任务感知摘要,禁滑动窗口;prompt 一图一节点
  一文件 + frontmatter(ADR-0004)
- **模型路由默认 strong**,降 light 仅限内部模式化步骤且须 eval 证明(ADR-0004)
- **观测 fail-open,写路径 fail-closed**(ADR-0005)
- **存储只有 Postgres**:checkpointer + 状态表 + pgvector + 审计共用一实例,Redis 不在
  agent 栈(ADR-0007)

## 4. 代码导览(目录 ↔ 职责)

四层骨架(见 `docs/standards/layering.md`)在本仓的落位,依赖**单向向下**:
`web(接口) → graphs(编排) → evaluation(评估域) → tools/state/kb/security(基础设施)`。

| 目录 | 层 | 职责 |
|---|---|---|
| `web/` | 接口层 | FastAPI 路由与入口横切:`routes`(chat SSE + 会话路由)、`config_admin`(配置管理面)、`kb_admin`(知识库管理面)、`evaluation_admin`(初筛管理面)、`auth`(鉴权依赖)、`session_store`(会话注册表,纯状态)、`agent_factory`(配置热生效)、`telemetry`(对话观测落账)、`app`(装配 + 生命周期 + trace 中间件) |
| `graphs/` | 编排层 | `assistant/`(A:ReAct 单循环 + 会话压缩)、`evaluation/`+`investigate_graph`(B 的编排面)、`copilot/`(C,规划)、`identity`(JWT→身份)、`router`(统一路由图,未接入) |
| `evaluation/` | 评估域 | 流水线领域逻辑:`runner`(job 执行)、`bundle`(出题证据线)、`explore`+`investigate_graph`(仓探索/出题)、`graph`(评分子图)、`judge`/`tech_stack`/`awards`(专项出题)、`scoring`(量规)、`schema`(数据契约)、`llm_common`(共享 LLM 小件)、`github_client`/`autograding`/`attribution`/`dossier` |
| `tools/` | 基础设施 | `client`(后端 REST 客户端,异常层级在此)、`readonly`/`knowledge`(语义化只读工具)、`write`(interrupt 写工具)、`credentials`(服务账号)、`interrupt_guard` |
| `state/` | 基础设施 | Postgres 状态面:`pg`(checkpointer 连接)、`threads`(会话档案)、`conversation`(对话日志)、`evaluation/`(分卡/job/评审队列,包)、`qbank`(题库)、`audit`(审计)、`config_store`(热配置) |
| `kb/` | 基础设施 | 知识库:入库分块/向量检索/表自举 |
| `security/` | 横切 | `pii`(脱敏)、`injection_guard`(数据区+注入扫描)、`fabrication_guard`(编造守卫) |
| `prompts/` | 资源 | prompt 唯一权威(一图一节点一文件 + frontmatter,ADR-0004) |
| `evals/` | 质量门 | eval 集 + 统一 runner(`python evals/run_evals.py`) |
| `observability.py` | 横切 | Langfuse 接线 + trace id(contextvar/W3C)+ PII 遮蔽 handler |

## 5. 关键流程

### 一次对话回合(A)

```
POST /chat(web/routes.chat,ChatBody 校验 + JWT 鉴权)
  → 会话注册表(session_store):命中/档案恢复/新建+建档
  → _stream_turn:配置热生效 → session 事件 → 模型并发闸 + 墙钟
  → 流消费(ReAct:assistant graph.astream)→ delta/tool 事件(PII 掩码逐块)
  → 工具调用(tools/*,只读直连后端;写操作 interrupt 确认)
  → 编造守卫(toolless)→ 轮末压缩 → conversation_log 落账 → sources/done
```

失败语义:闸满/超时/断连/异常四路都收敛到同一落账点;客户端只收
`error(code,message)` 稳定事件,原始异常只进服务端日志。

### 一个评估 job(B)

```
POST /admin/evaluation/run(evaluation_admin,权限 evaluation:run)
  → runner.submit:后端权威归属核对 → create_jobs(幂等,活跃去重)
  → _execute_job:评分段(_do_scoring:取数→归属硬断言→脱敏→评分子图→落卡)
  → 题库段(_do_qbank:出题 bundle→qbank 落库→用量日志)
  → mark succeeded(带 qbank_status)→ 完成审计 → 评审队列(list_review_queue)
```

失败语义:评分失败 job 落 failed(可重试,上限交人工);题库线失败不影响评分卡
(job succeeded + qbank_status=failed,管理面可见真实原因)。

## 6. 工具 ↔ 端点映射(要点)

完整映射以后端 `openapi.yaml` 为唯一真实来源;结构性约定:

- 只读工具注册进 MCP(对外);`get_my_interview` 等需**最终用户本人令牌**,仅在
  agent 进程内装配
- 写工具(`assign_interview`/`handle_reschedule`/`submit_resume_score`)仅进程内装配,
  全部经 interrupt 指纹令牌确认(ADR-0005)
- 工具粒度对齐意图而非接口;返回做投影裁剪,错误信息必须可行动

## 7. 状态与安全

- **存储只有 Postgres**(ADR-0007):checkpointer、会话档案(agent_threads)、
  对话日志、评测三表(分卡/job/题库)、审计、kb 向量
- **PII 是分层边界**:进模型前脱敏(tools 出口)+ trace 采集侧遮蔽(observability
  handler)+ 模型输出侧守卫 + 挂起载荷 TTL
- **不可信输入**:简历/评测记录/候选人自述进 prompt 一律包数据区;批判节点兼作
  注入检测
- **写操作三重闸**:工具装配(读不到)→ interrupt(执行不了)→ 指纹令牌(绕不过)
- **审计**(ADR-0006):acting_user / agent 模块+节点+prompt 版本 / action+指纹 /
  decision / trace_id

## 8. 评估与可观测

- eval 分层与门禁见 `evals/README.md`;评测线可观测 runbook 见
  `docs/eval-observability.md`;prompt 事实源=文件 frontmatter,Langfuse 只读镜像
- trace 串联:轮级 trace id(web 中间件/CLI/eval job 三入口)+ 出站 traceparent +
  审计 trace_id 四面同 id;日志行自带 `[trace_id]`
- 工程规范(分层/错误处理/日志/注释)见 `docs/standards/`

## 文档地图

- 代码架构详解(分层/包结构/设计模式/流程图):`docs/architecture.md`
- 决策记录(ADR):`docs/adr/`(工作区根)
- 工程规范:`docs/standards/`(入口 README 有加载地图)
- 重构计划与进度:`docs/refactor-roadmap.md`
- 历史过程记录(评审纪要/谈判清单/里程碑规划):`docs/archive/`(仅追溯用)
- 架构决策的背景与备选:git 历史与 ADR;本文件只描述**当前**形态
