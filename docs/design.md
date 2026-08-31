# 设计方案

> v0.2 · 2026-08-25。v0.1(2026-08-21)为初始设计;本版已并入 PR #66 架构评审的全部采纳决议
> (ADR-0003~0007 与 12 条端点漂移修正)。与 ADR 冲突时以 ADR 为准;工具↔端点映射以
> 后端仓库 `openapi.yaml` 为唯一真实来源。

## 1. 愿景与目标

**愿景**:把招新季"人肉串流程"的部分交给 agent——候选人有问题不用等管理员翻后台,
简历初筛不再是纯人工逐份过,面试官不用一边提问一边低头填表。同时,本仓库是一次完整的
agent 工程实践:ReAct 工具调用、状态机编排、human-in-the-loop、记忆、评估飞轮,
都要在真实业务上落地。

**目标**

- 落地三个能力模块(A 对话助理 / B 评估流水线 / C 面试官 Copilot),共用一套工具层与
  状态设施,而非三个孤立脚本
- 与后端**完全解耦**:只通过 REST API + 最小权限服务账号 + `X-On-Behalf-Of` 代理身份
  (ADR-0006)交互,不触碰数据库
- 写操作一律经 LangGraph `interrupt` 人工确认,令牌=interrupt 恢复凭证(操作指纹绑定、
  一次性,ADR-0005);agent 只加速流程、不替人拍板
- 从第一个 PR 起就有 trace(Langfuse)和 eval 集,质量可回归、变更可门禁
- 每笔 agent 行为可审计、可回溯:归因到最终用户 + 执行模块/节点/prompt 版本(ADR-0006)

**非目标**

- **不做自动录取决策**——评估流水线输出是"辅助初筛参考",最终决定永远由人做
- 不改造后端核心业务逻辑;不接管排期分配算法(方案B)
- 第一期候选人侧只读
- 不用 LangChain 经典 Chain/AgentExecutor;编排全部走 LangGraph,langchain-core 只作
  model/tool 接口层

## 2. 三个能力模块

三者是一条价值链:**B 产出的候选人摘要与定制面试题,在 C 中被面试官消费;C 产出的评价
回流后端评价看板;A 是管理员触发 B、查看进度与数据的对话入口。**

### A · 招新对话助理

| 角色 | 典型请求 | 能力边界 |
|---|---|---|
| 候选人 | "我的面试安排在什么时候?""简历什么状态?" | 只读 + RAG 问答;仅能查自己 |
| 管理员 | "明天下午还有空场次吗?""待处理的改期列一下""把李四调剂到周六上午" | 读全量;写操作经 interrupt 确认 |
| 管理员 | "生成本周投递与面试进度周报" | 多工具取数 + 报告合成 |

**入口**(ADR-0003):CLI(M1,开发调试)→ 官网 SSE + 管理端对话面板(M5,含确认/拒绝
上行端点,#8)。**飞书入口(INF-05..07)保留为 M3 一等入口**(2026-08-31 拍板:
管理员在飞书群 @ 机器人查数据、卡片确认写操作;TOOL-08 多维表格写回随之保留,
EVA-08 报告推送依赖它)。

**形态**(ADR-0003):无意图分类。身份解析(确定性)→ 按角色装配工具集(SEC-02)→
ReAct 单循环;"意图"由模型选工具隐式表达。

### B · 简历评估流水线

输入一个招募周期的待筛简历,对每份产出:结构化摘要(技能/项目/佐证标注)、分部门量规
评分(每维度必须附引用简历原文的"依据",schema 见 #69)、3~5 个定制面试题。

**触发**(共识,#34):流水线本体=graph + 调度队列;触发面=对话遥控器工具
(`trigger_evaluation` / `get_evaluation_progress`)+ 管理端"开始审核"按钮 + cron,
全部汇入同一队列。**对话是遥控器,不是流水线本身。**

**写回**:批量写回走评审确认语义(与对话令牌是两套机制,不可互替,ADR-0005);
维度分+依据的落库通道在 SEC-01 谈判清单内(#52),谈判前落 agent 侧 Postgres + 飞书表格。

### C · 面试官 Copilot

宿主:**官网管理端面试页**(界面即路由,直连 copilot 三态图;不经 A 前门)。

1. **面试前**:候选人卡片(评价看板简历 + B 的摘要与面试题)——`get_candidate_card`
   依赖 X-On-Behalf-Of 落地(服务账号直调会被场次绑定校验拒绝)
2. **面试中**:速记 → 低延迟追问建议(零工具调用保证速度)
3. **面试后**:速记按评价维度归纳成评价表草稿,面试官确认后提交

**C+ 语音模式**(M6,#43~45):流式 ASR(说话人分离、静默窗口触发)替换速记输入源,
图与追问逻辑复用;录音知情同意与留存策略(COP-08)**先行**于 ASR 接入;转写是新的
不可信输入面。

## 3. 总体架构

```
A:CLI / 官网 SSE(飞书悬置) ─→ 身份解析 → 按角色装配工具集 → ReAct 单循环
B:调度队列(遥控器工具/按钮/cron)─→ evaluation 图(批处理)
C:官网管理端面试页 ─→ copilot 三态图
                  │
     工具层 tools/(进程内直连 Python 函数;MCP Server 仅对外暴露)
                  │
     后端 REST(服务账号 JWT + X-On-Behalf-Of)
     Postgres(checkpointer / pgvector / 审计) · Langfuse(trace,fail-open)
```

关键决策(详见对应 ADR):

- **入口各自直连,无统一意图路由**(ADR-0003)
- **工具进程内直连**;MCP 纯对外给 Claude Code 等挂载(ADR-0003)
- **上下文四段组装 + 双 cache 断点**;超阈值任务感知摘要,禁滑动窗口;prompt 一图一节点
  一文件 + frontmatter(ADR-0004)
- **模型路由默认 strong**,降 light 仅限内部模式化步骤且须 eval 证明(ADR-0004)
- **观测 fail-open,写路径 fail-closed**(ADR-0005)
- **存储只有 Postgres**:checkpointer + 长期记忆/pgvector + 审计共用一实例,Redis 不在
  agent 栈;部署 Node B,后端 MySQL 不迁移,Langfuse 生产部署延后(ADR-0007)

## 4. 工具 ↔ 端点映射(已按 openapi 核对,2026-08-24 修正)

### 只读(注册进 MCP,get_my_interview 除外)

| 工具 | 后端端点 | 备注 |
|---|---|---|
| `get_open_cycle` | GET /api/cycles/open | 返回无 status 字段 |
| `search_resumes` | GET /api/resumes/search | department→expectedDepartment;另有 name/major/status/分页 |
| `get_resume_detail` | GET /api/resumes/admin/{userId}/{cycleId} | 无按 resumeId 直查的端点 |
| `get_my_interview` | GET /api/interview/schedule/my | cycleId 必填;需本人令牌,**不入 MCP** |
| `find_available_sessions` | GET …/cycles/{id}/available-sessions | 后端仅 deptId 过滤,date 客户端过滤 |
| `list_unassigned` | GET …/cycles/{id}/unassigned | |
| `list_reschedule_requests` | GET /api/interview/reschedule/admin/list | cycleId 必填;status int 0/1/2 |
| `get_recruit_statistics` | result/list + evaluation summary 聚合 | 原 /statistics 未实现,端点列入 SEC-01 谈判 |
| `get_candidate_card` | …/candidates/{scheduleId}/resume + …/dimensions | 需 X-On-Behalf-Of |

### 写(仅 agent 进程内装配,全部经 interrupt 指纹令牌)

| 工具 | 后端端点 | 备注 |
|---|---|---|
| `assign_interview` | POST …/preferences/{resumeId}/assign | 分配/再分配;满员业务码 3604;改期后重排也走它 |
| `handle_reschedule` | PUT …/reschedule/admin/{id}/handle | status 1 同意 / 2 拒绝 + adminNote;同意不自动重排 |
| `submit_resume_score` | PUT /api/resumes/{id}/score | int 0~100;维度分+依据通道待 SEC-01 |

(v0.1 的 `move_interview` 已砍除:后端 manual-adjust 端点 deprecated。)

## 5. 状态与记忆

- **v1 只有 Postgres checkpointer**(#46):会话状态、interrupt 挂起载荷、C 速记、
  B 断点续跑;thread 生命周期契约见 #67(命名/属主/并发串行化/TTL/跨入口)
- 长期记忆 MEM-02..05 收缩为"等第一个真实需求再启"(P2)
- **RAG Day1 接口化**(#51):先定 RAG 形接口 + 小语料实现,后端实现可换

## 6. 安全与合规

- **PII 是分层边界,不是单个节点**(#68,红线):进模型前脱敏 + trace 采集侧防原文入
  Langfuse + 模型输出侧守卫 + 挂起载荷 TTL;占位符映射契约随 #68 定义
- **不可信输入**:简历、(未来)转写在 prompt 中框定为数据区;EVA-05 证据核验节点
  (代码核引用真实性于脱敏文本内 + 模型核维度矛盾)兼作注入检测
- **写操作三重闸**:工具装配(读不到)→ interrupt(执行不了)→ 指纹令牌(绕不过)
- **代理身份与审计**(ADR-0006):审计行五字段(acting_user / agent 模块+节点+prompt
  版本 / action+指纹 / decision / trace_id);SEC-01 后端谈判清单终版见 #52(7 项一次谈)
- 真实凭证只存 `.env`;agent 永不持有 JWT_SECRET

## 7. 评估与可观测

- Langfuse 自托管:M1~M2 跑开发机(观测 fail-open),生产部署招新季前再定(#58)
- eval 三层:工具选择/参数(确定性,进 CI)→ 终答 LLM-as-judge;简历标注集 20~50 份
  (不入库);badcase 回流为回归用例;可执行性方案(secrets/数据集分发/基线/judge 校准)
  见 #70;prompt 事实源=文件 frontmatter,Langfuse 只读镜像(ADR-0004)

## 8. 里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1(1~2周) | 工具层实现 + MCP Server + CLI ReAct(管理员侧)+ Langfuse 接线 + 最小 eval;前置:SEC-01 后端谈判(#52) | Claude Code 挂 MCP 查"技术部还有多少待筛简历"答对;CLI 完成多工具组合查询 |
| M2(2周) | B 流水线(解析→脱敏→评分→证据核验→出题)+ 分数卡 schema 与评审确认(#69)+ PII 边界(#68)+ 标注集与 eval 门禁(#70) | 对历史周期影子运行,产出与人工初筛的一致率报告 |
| M3(2~3周) | Postgres checkpointer(#46)+ thread 契约(#67)+ interrupt 确认闭环 + 审计行;入口范围按"A 飞书悬置"的结论执行 | 一次"查空场次→assign_interview→确认执行"全流程 |
| M4(2周) | C 三态闭环(卡片/速记建议/评价草稿,消费 M2 产出)+ RAG 接口化(#51) | 一场演练面试全程使用 Copilot |
| M5(2周) | 官网双端:SSE chat(含确认/拒绝上行,#8)+ 候选人浮窗 + 管理端面板(前端配合,契约先冻结) | 候选人在官网完成只读问答;管理员页面内完成带确认写操作 |
| M6(2~3周) | C+ 语音:合规先行(COP-08)→ 流式 ASR → 静默窗口实时追问 | 演练中追问采纳率 ≥ 50% |

## 9. 开放问题

1. ~~A 是否接飞书~~ **已拍板(2026-08-31):保留**,INF-05..07 与 TOOL-08 全保留
2. SEC-01 后端谈判结果——决定代理身份两层防线还是单防线退路,以及 B 落库通道
3. trace PII 的具体修法(工具返回层脱敏 vs 采集点二次脱敏)——#68 内拍板
4. 审计行权威存储(agent PG / 后端端点 / 文件+聚合)——随 SEC-01 谈判定

## 文档地图

- 决策记录:`docs/adr/0003`(入口与编排)/ `0004`(上下文工程)/ `0005`(令牌与失败
  哲学)/ `0006`(代理身份与审计,Proposed)/ `0007`(存储,Accepted)
- 评审产物:`docs/grill-session-2026-08-24.html`(32 项决策总览)/
  `docs/gap-scan-2026-08-24.md` / `docs/sync-agenda-2026-08-24.md`
- 历史版本:v0.1 完整稿(含被取代的意图分类、Redis 方案)存于设计 artifact,不入库
