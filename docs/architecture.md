# 代码架构详解

> **这是什么**:本仓代码结构的深度导览——分层、包结构、设计模式、复杂业务逻辑的图形化表示。
> **何时读**:接手模块、评审设计、排查跨层问题之前;`docs/design.md` 讲"做什么与为什么",
> 本文讲"代码怎么组织、怎么流转"。
> 图为 Mermaid,与代码的对应关系以**符号名**标注,重构后以代码为准。

## 1. 总览:四层骨架 + 三个能力模块

```mermaid
flowchart TD
    subgraph 接口层["接口层 web/"]
        R["routes<br/>chat SSE + 会话路由"]
        AUTH["auth<br/>鉴权依赖"]
        CA["config_admin<br/>配置管理面"]
        KA["kb_admin<br/>知识库管理面"]
        EA["evaluation_admin<br/>初筛管理面"]
        RA["retrospective_admin<br/>周期复盘管理面"]
        SS["session_store<br/>会话注册表(纯状态)"]
        AF["agent_factory<br/>配置热生效"]
        TEL["telemetry<br/>对话观测落账"]
        APP["app<br/>装配 + 生命周期 + trace 中间件"]
    end
    subgraph 编排层["编排层 graphs/"]
        AG["assistant/<br/>A:ReAct 单循环"]
        IDT["identity<br/>JWT → 身份"]
        RTS["router<br/>统一路由图(未接入)"]
        COPILOT["copilot/(规划)"]
    end
    subgraph 评估域["评估域 evaluation/"]
        RUN["runner<br/>job 执行"]
        BDL["bundle<br/>出题证据线"]
        EXP["explore + investigate_graph<br/>仓探索/出题"]
        SCG["graph<br/>评分子图"]
        LLMC["llm_common<br/>共享 LLM 小件"]
        MORE["judge / tech_stack / awards / scoring / schema ..."]
    end
    subgraph 复盘["周期复盘 retrospective/"]
        RM["catalog / metrics / guard / compare<br/>指标目录·采集·小格子抑制·跨周期差值"]
    end
    subgraph 基础设施层["基础设施层"]
        TLS["tools/<br/>后端 REST 客户端 + 语义化工具"]
        ST["state/<br/>Postgres 状态面"]
        KB["kb/<br/>pgvector 知识库"]
        SEC["security/<br/>PII / 注入守卫 / 编造守卫"]
    end
    R --> AG
    R --> SS
    R --> TEL
    R --> AF
    EA --> RUN
    RA --> RM
    RM --> TLS
    RM --> ST
    AG --> IDT
    AG --> TLS
    BDL --> EXP
    RUN --> SCG
    RUN --> BDL
    EXP --> TLS
    TLS --> ST2["后端 REST API"]
    ST --> PG[("Postgres<br/>checkpointer/状态表/pgvector/审计")]
    KB --> PGV[("pgvector")]
```

三个能力模块共用同一套工具层与状态设施(ADR-0003:入口各自直连,无统一意图路由):

| 模块 | 入口 | 编排 |
|---|---|---|
| A 对话助理 | 官网 SSE / CLI | `graphs/assistant` ReAct 单循环 |
| B 评估流水线 | 管理面 / 启动自动恢复 | `evaluation/runner` + LangGraph 子图 |
| C 面试官 Copilot | 官网管理端(规划) | `graphs/copilot`(预留) |

三个模块之外还有一条管理面触发的离线批处理:**周期指标快照**(`retrospective/`,纯代码聚合,
不含模型),见 §11。

## 2. 包结构

```
src/official_agent/
├── web/                    # 接口层:FastAPI 路由与入口横切
│   ├── app.py              #   create_app:lifespan(自举/恢复/后台任务)+ trace 中间件 + 路由注册
│   ├── routes.py           #   /chat SSE、/sessions 会话路由、/admin/conversations 运营视图
│   ├── auth.py             #   authenticate(JWT→身份)+ require_any 权限工厂(唯一出处)
│   ├── session_store.py    #   进程内会话注册表:TTL+LRU 淘汰、删除中协调(零 FastAPI 依赖)
│   ├── agent_factory.py    #   配置指纹比对 → agent 热重建
│   ├── telemetry.py        #   conversation_log 落账、KB 引用锚收集、usage/前缀指纹
│   ├── config_admin.py     #   /admin/config:低敏热改 + 高敏掩码 + base_url 白名单
│   ├── kb_admin.py         #   /admin/kb*:知识条目 CRUD/入库重嵌
│   ├── evaluation_admin.py #   /admin/evaluation*:触发/队列/维卡/采纳/驳回/题库
│   └── retrospective_admin.py # /admin/retrospective/*:指标快照采集与读取;报告生成/审阅/发布
├── graphs/                 # 编排层:LangGraph 图
│   ├── assistant/          #   A:build_assistant_agent(create_agent ReAct)+ 会话压缩
│   ├── identity.py         #   JWT → ResolvedIdentity(权限码/角色/属主)
│   ├── router.py           #   统一路由图(预留,未接入)
│   └── copilot/            #   C:面试官 Copilot(预留)
├── evaluation/             # 评估域:流水线领域逻辑
│   ├── runner.py           #   EvaluationRunner:submit/派发/_execute_job(评分段+题库段)
│   ├── graph.py            #   评分子图:precheck→(hard_zero? finalize_hard : llm_score)→finalize
│   ├── investigate_graph.py#   调查出题子图:route→explore→generate(含 skip/guided 分支)
│   ├── bundle.py           #   出题证据线:仓线/评测错因线/奖项线/兜底线 → qbank 信封
│   ├── explore.py          #   受限 ReAct 仓探索(轮数/墙钟双闸 + dossier 槽位)
│   ├── judge.py / tech_stack.py / awards.py   # 专项出题
│   ├── scoring.py          #   绝对卡短路 + 清单派生 0-10 分
│   ├── grading.py          #   同部门候选池内相对分级(优秀/良好/一般)+ 调剂建议/面试提示
│   ├── applicant.py        #   报名信息解析(志愿部门/专业/年级)
│   ├── schema.py           #   数据契约(ScorecardOutput/QbankV2/QuestionGroupV2...)
│   ├── llm_common.py       #   共享小件:content_text/prompt_version/温度定档/invoke_with_retry
│   ├── runner 依赖的 github_client / autograding / attribution / dossier
│   └── state/evaluation/(见 state/)
├── state/                  # 基础设施:Postgres 状态面(Repository 语义)
│   ├── pg.py               #   checkpointer 连接 + 挂起载荷清理
│   ├── threads.py          #   agent_threads:会话档案(建档/属主校验/硬删)
│   ├── conversation.py     #   agent_conversation_log:对话日志 + usage 解包
│   ├── evaluation/         #   包:scorecard_store / job_store / review_queue / bootstrap / _connection(池)
│   ├── qbank.py            #   interview_qbank:题库 + pick_log
│   ├── audit.py            #   agent_audit_log:审计(五字段,ADR-0006)
│   ├── config_store.py     #   agent_config:热配置(DB 覆盖 env)
│   └── retrospective/      #   包:snapshot_store(cycle_metrics_snapshot,只追加)/ aggregates(各域只读聚合)
│                           #       / report_store(cycle_retrospective:报告版本与发布状态机)
├── tools/                  # 基础设施:后端 REST 的语义化封装
│   ├── client.py           #   BackendClient + 异常层级(AuthError/UnavailableError/BackendError)
│   ├── readonly.py         #   只读工具(按意图命名,60 秒按来问者缓存)
│   ├── projection.py       #   工具返回投影(只留回答要用的字段,姓名/打分不进)
│   ├── envelope.py         #   统一信封 {status: ok|error|unavailable, hint}
│   ├── write.py            #   写工具(官网通道:确认卡片 → 指纹核对 → 意图审计 → 本人令牌执行 → 结果审计)
│   └── knowledge.py        #   search_knowledge(知识库检索,降级契约;解析开放周期、落检索日志)
├── kb/                     # 知识库:chunking / embedding / keywords(分词) / schema / store(混合检索+周期过滤)
│                           #   / query_log(kb_query_log:每次检索的结局与门槛前最高分)
├── memory/                 # 长期记忆:store(agent_memory 表) / extract(后台提取+闸门) / recall(注入 system 末尾)
├── retrospective/          # 周期复盘:catalog(指标目录:id/名字/口径) / metrics(各域口径函数+collect_metrics)
│                           #   / guard(小格子抑制) / compare(跨周期差值,跨指标定义版本拒绝)
│                           #   / views(分析员的指标视图) / roles(四个角色的输出结构与闸门)
│                           #   / grounding(数字落地守卫) / graph(扇出→撰稿→守卫回炉→成稿)
│                           #   / render(Markdown 渲染) / reports(后台生成任务)
├── security/               # 横切:pii(脱敏)/ injection_guard(数据区+扫描)/ fabrication_guard
├── prompts/                # prompt 唯一权威(一图一节点一文件 + frontmatter,ADR-0004)
├── evals/                  # eval 集 + 统一 runner(python -m evals / evals/run_evals.py)
├── observability.py        # Langfuse 接线(fail-open)+ trace id(contextvar/W3C)+ PII 遮蔽 handler
├── config.py               # Settings(env)+ HOT_KEYS + get_effective_settings(DB 覆盖)
├── credentials.py          # 服务账号凭证(0600 文件)
├── logging_conf.py         # 日志(stdout+落盘,TraceIdFilter 注入 trace_id)
└── cli.py / mcp_server.py  # CLI 入口 / MCP 对外暴露(维护态)
```

## 3. 依赖规则

```mermaid
flowchart LR
    WEB["web 接口层"] --> GRAPHS["graphs 编排层"]
    GRAPHS --> EVAL["evaluation 评估域"]
    GRAPHS --> INFRA["tools / state / kb / security"]
    EVAL --> INFRA
    OBS["observability / logging / config(横切)"] -.-> WEB & GRAPHS & EVAL & INFRA
```

- **单向向下**,禁止反向与跨层跳跃;领域规则不 import 基础设施实现细节
- **接口层只做编排**:校验 → 调用 → 组装;业务规则在 evaluation/编排层,不在 route
- **基础设施不感知上层**:tools 不知道 graphs,state 不知道 web
- 例外与边界:web/telemetry 可以 lazy import `state.conversation`(观测面,函数内导入保持
  "web 入口无 PG 可跑"纪律)
- `retrospective/` 与 `evaluation/` 同级:由 web 调用,向下只依赖 tools/state/security,
  以及 `evaluation.schema` 里的部门清单常量;不 import 任何图或 runner

## 4. 设计模式与惯用法(模式 → 落点)

| 模式/惯用法 | 落点 | 说明 |
|---|---|---|
| **权限工厂**(Factory + 闭包) | `web/auth.require_any(*codes)` | 生成 FastAPI 依赖;单码直接 `require_any("code")`,消灭五份复制品 |
| **构造器注入** | `agent_factory.ensure_fresh_agent(build_agent=...)` | 重建构造器由调用方注入,模块不绑定装配实现 |
| **双检锁单例** | `evaluation/bootstrap.ensure_once`、`_connection.get_pool`、`routes._get_model_gate` | 每进程一次的自举/懒建资源 |
| **哨兵对象** | `job_store._Unset` | 区分「没传这列」与「显式置 NULL」,mark_job 只更新显式列 |
| **状态机** | evaluation_job / scorecard | 见 §6.4;部分唯一索引兜底活跃唯一性 |
| **管道/子图** | LangGraph 三图 | 见 §6.2/6.3;节点纯函数 `(state) -> dict` |
| **校验回灌重试** | `llm_common.invoke_with_retry` | 只捕 ValueError 回灌模型自纠;程序缺陷向上暴露 |
| **降级双轨** | 观测 fail-open / 写路径 fail-closed(ADR-0005) | 每处降级必须留痕(日志),注释说明为什么安全 |
| **Repository 语义** | `state/*_store` | 方法名说业务(`find_active_users` 式),SQL 不上浮 |
| **投影(读模型)** | `review_queue.list_review_queue` | DISTINCT ON 最新卡 + LATERAL 归属/决策历史 |
| **装饰式包装** | `_PiiMaskedLangfuseHandler` | 委托 Langfuse handler,消息/载荷统一脱敏(fail-closed) |
| **端口适配** | `tools/client.py`(后端 REST)、LLM 统一经 `build_model` | 第二实现出现才抽接口(YAGNI,ADR-0009) |
| **参数注入时钟** | `session_store.evict_locked(now)`、压缩判定 | 可测性根基:测试不 sleep |

## 5. 对话回合(A 模块主链路)

```mermaid
sequenceDiagram
    participant C as 客户端
    participant W as web/routes
    participant SS as session_store
    participant AF as agent_factory
    participant A as assistant graph
    participant TL as tools/(后端 REST)
    participant T as telemetry

    C->>W: POST /chat(message, session_id?)
    W->>W: JWT 鉴权 + ChatBody 校验
    W->>SS: 取会话(内存命中/档案恢复/新建建档)
    W->>AF: 配置指纹比对(变则重建 agent)
    W-->>C: event: session(created)
    W->>W: 模型并发闸 + 墙钟超时
    loop 流式消费(astream 双模式)
        A->>TL: 工具调用(只读直连/写操作 interrupt)
        Note right of TL: search_knowledge 每查一次<br/>后台落一行 kb_query_log(fail-open)
        A-->>W: delta 块(PII 掩码逐块) / tool 事件 / usage 终块
        W-->>C: event: delta / tool
    end
    opt toolless 回复
        W->>W: 编造守卫 → 改写/放行
    end
    W->>W: 轮末压缩(超阈值,摘要回写 checkpoint)
    W->>T: conversation_log 落一行(带轮级 trace_id 与守卫判定;to_thread,fail-open)
    W-->>C: event: done(带 sources 引用锚) 或 error(稳定文案+trace_id)
```

失败收敛:闸满(busy)/超时(timeout)/断连(client_disconnected)/异常(按异常类型分类)
五条路径都收敛到**同一落账点**,客户端只见稳定错误码。

## 6. 评估流水线(B 模块)

### 6.1 job 生命周期

```mermaid
flowchart TD
    S["POST /admin/evaluation/run"] --> AUTH["权威归属核对<br/>(后端按简历号派生 user_id)"]
    AUTH -->|"错位 → SubmissionDataError → 400"| FAIL
    AUTH --> CJ["create_jobs(幂等:活跃 job 复用<br/>部分唯一索引兜底,逐条事务)"]
    CJ --> D["派发(_sem 并发闸)"]
    D --> P["pending"]
    P --> R["running(attempts+1,简历状态 6)"]
    R --> SC["评分段 _do_scoring<br/>取数→归属硬断言→脱敏→评分子图→落卡"]
    SC --> QB["题库段 _do_qbank<br/>出题 bundle→落库→用量日志"]
    QB -->|成功| OK["succeeded<br/>(card_version + qbank_status)"]
    QB -->|题库线失败| OKF["succeeded + qbank_status=failed<br/>(评分卡有效,原因直达管理面)"]
    SC -->|失败| F["failed(error 摘要,<br/>简历状态回 2,可重试)"]
    F -->|requeue(attempts<3)| P
    F -->|attempts 达上限| MAN["人工队列"]
```

```mermaid
stateDiagram-v2
    [*] --> pending: create_jobs
    pending --> running: 派发(attempts+1)
    running --> succeeded: 评分+题库完成
    running --> failed: 异常(错误摘要落行)
    failed --> pending: requeue_failed / requeue_stale(attempts<3)
    failed --> [*]: attempts 达上限(转人工)
    succeeded --> [*]
```

### 6.2 评分子图(hard_zero 短路)

```mermaid
flowchart LR
    PRE["precheck<br/>(确定性绝对卡规则)"] -->|"hard_zero"| FH["finalize_hard<br/>零卡不调模型<br/>(两张清单全未达成+命中原因)"]
    PRE -->|"正常"| LS["llm_score<br/>(校验回灌重试 ×2)"]
    LS --> FIN["finalize<br/>统一卡形状"]
    FH --> FIN
    FIN --> E(["END"])
```

初筛看两个维度,互不折算:**部门匹配度**(四个部门的录入标准清单都判,志愿部门那张
决定匹配度,其余用于调剂建议)与**认真程度**(内容充实/了解社团部门/意愿真挚/成文/
不套话)。模型只逐项判定达成与否并给原文引文,两维 0-10 分由清单权重派生落卡;
「优秀/良好/一般」**不落库**,读取时由 `grading.grade_pool` 按「周期 + 第一志愿部门」
分池相对划分(头尾各约两成;池子不足 15 人改用绝对锚点;媒体部与综合部各自成池)。
数值分仅 `evaluation:score:view` 可见。

### 6.3 调查出题子图(route → explore → generate)

```mermaid
flowchart TD
    RT["route_node<br/>归属四级瀑布:钉仓→URL→贡献声明→登录名<br/>unverified 不深挖(ADR-0008)"] -->|deep_dive / cv_dive| EX["explore_node<br/>受限 ReAct:80 轮/300s 双闸<br/>十类取材槽位(dossier)<br/>工具返回过注入守卫"]
    RT -->|guided| GEN["generate_node<br/>出题+校验回灌 ×2<br/>(只捕 ValueError)"]
    RT -->|skip| SKIP["skip_node<br/>空组+mode=skipped"]
    EX --> GEN
    GEN --> FIN["finalize"]
    SKIP --> FIN
    FIN --> E(["END"])

    GEN -.两次不合规.-> ERR["ValueError → error 态<br/>(bundle 降级为错误组,不炸整条)"]
```

explore 的工具执行内循环(`_run_tool_calls`):逐调用判**轮数/墙钟双硬闸**,dossier 槽位满
则静默丢弃并回执模型;ToolMessage 严格紧跟 tool_calls(OpenAI 消息序纪律)。

### 6.4 scorecard 状态机

```mermaid
stateDiagram-v2
    [*] --> draft: save_scorecard(版本 MAX+1,旧版保留)
    draft --> adopted: 采纳(评审本人向后端投一票)
    draft --> rejected: 驳回
    adopted --> draft: 复评(新版本卡)
    rejected --> draft: 复评(新版本卡)
```

## 7. 会话生命周期(web)

```mermaid
flowchart TD
    REQ["携 session_id 续聊 / 无 id 新聊"] --> DEL{"在 deleting 集合?"}
    DEL -->|是| R409["409 会话正在删除"]
    DEL -->|否| HIT{"内存命中?"}
    HIT -->|是| OWN{"属主一致?"}
    OWN -->|否| R403["403"]
    OWN -->|是| TOK{"token 轮换?"}
    TOK -->|是| RB["重建 agent(绑新 token)"]
    TOK -->|否| TOUCH["touch(LRU)"]
    RB --> TOUCH
    HIT -->|否 + 有 id| REC["档案恢复:resolve_thread<br/>(属主/active 校验,fail-closed 503)"]
    REC -->|不存在/跨属主| R404["404(不区分,防枚举)"]
    REC -->|通过| REB["重建运行时对象(checkpointer 必须可用)"]
    HIT -->|否 + 无 id| NEW["create_thread 建档<br/>(PG 不可用降级随机会话,留痕)"]
    NEW --> PUT["registry.put + evict(TTL+LRU,在途不淘汰)"]
    REB --> PUT
    PUT --> GO["进入对话回合(turn_lock 串行化)"]
    GO --> BUSY2["第二并发请求 → busy 立即回"]
```

## 8. 数据模型(Postgres,agent 侧)

| 表 | 内容 | 关键约束 |
|---|---|---|
| `agent_threads` | 会话档案(thread_id/属主/状态) | thread_id 全局唯一;软删+保留期清理 |
| `agent_conversation_log` | 对话日志(问题摘要/工具/耗时/usage/轮级 trace_id/守卫判定) | fail-open 写入 |
| `evaluation_scorecard` | AI 参考分卡(card JSONB/total/hard_zero) | UNIQUE(resume,cycle,card_version);状态机 CHECK |
| `evaluation_job` | 执行 job(状态机/attempts/qbank_status) | 部分唯一索引:活跃 job 每 (resume,cycle) 一条 |
| `interview_qbank` | 题库信封(v2 JSONB) | 版本递增 |
| `qbank_pick_log` | 面试官选题记录 | 服务端权威引用(防伪造证据路径) |
| `agent_audit_log` | 审计(ADR-0006 五字段) | 代理身份+trace_id |
| `agent_config` | 热配置低敏键 | 白名单 HOT_KEYS |
| `kb_source` / `kb_faq` / `kb_doc` / `kb_chunks` | 知识库来源/内容/向量 | 禁跨 embedding 模型混排;`kb_source.cycle_id` 非空的条目只在该招新周期开放时进检索 |
| `kb_query_log` | 对话里每次知识库检索一行(脱敏后的问题/结局/门槛前最高分/命中条目/生效周期) | fail-open 写入;按 `thread_id` 随会话删除;`trace_id` 关联对话日志 |
| `cycle_metrics_snapshot` | 每个招新周期的指标快照(过了小格子抑制的聚合数 + 当时的指标目录) | UNIQUE(cycle,version),只追加;没有 `thread_id`,不随会话删除或保留期清理 |
| `cycle_retrospective` | 周期复盘报告(结构化正文/Markdown/被守卫删掉的句子/prompt 版本) | UNIQUE(cycle,version);状态机 CHECK;部分唯一索引:每周期至多一份已发布、一份在生成 |

## 9. 横切机制

- **trace 串联**:三入口(web 中间件/CLI/eval job)置轮级 id(归一 W3C 32-hex)→
  出站 `traceparent` → 审计 trace_id → 日志 `[trace_id]`,四面同 id(ADR-0012)。
  对话日志与知识库检索日志存的是入口那个 id(`observability.turn_trace_id`),两表按它
  关联;Langfuse 开启时图内取到的是 span 的 id,与入口 id 不同(见
  `docs/proposals/kb-ops-loop.md` D1)
- **PII 分层边界**:工具出口 `mask_pii_deep` → trace 采集侧遮蔽(handler)→ 模型输出
  侧守卫 → 挂起载荷 TTL
- **错误分类**:类型判定(`BackendAuthError`/`BackendUnavailableError`/`BackendError`)→
  稳定错误码;受控领域文案是对外契约,未预期异常原文不出边界(ADR-0011)
- **配置热生效**:PUT /admin/config → DB upsert + 缓存失效 → 每轮指纹比对 → 变则重建
  agent(`agent_factory`,读取失败留痕降级)

## 10. 测试与质量门禁

| 层 | 位置/命令 | 把关内容 |
|---|---|---|
| 单测 | `uv run pytest`(709+) | 确定性逻辑:工具/图结构/权限/脱敏/状态面(mock 连接) |
| 集成 | pgvector 容器 + `POSTGRES_URL=... pytest tests/*_pg.py tests/test_kb_integration.py` | DDL 幂等/唯一索引/事务边界/锁纪律(自 SKIP 不算通过) |
| eval | `python evals/run_evals.py` | 工具选择/参数红线(forbidden_tools)、阈值门禁、LLM-as-judge |
| 机械 | `uv run ruff check .` | lint(line-length 100) |

评分分级可见与客服面红线有专门保证测试:`test_no_role_assembles_score_tools`
(装配层)、`test_assistant_surface_never_imports_evaluation_store`(tripwire)、
`ts-score-not-exposed-*`(eval 行为红线)。

## 11. 周期指标快照(管理面触发的离线批处理)

```mermaid
flowchart TD
    T["POST /admin/retrospective/snapshot<br/>(evaluation:run)"] --> W["取周期起止日期<br/>GET /api/cycles/{id} → 北京时间时间窗"]
    W -->|后端查不到周期| NF["404,不落快照"]
    W --> C["collect_metrics:逐域取数"]
    C --> F["漏斗:后端统计(服务账号)"]
    C --> A["对话 / 知识库:按时间窗聚合"]
    C --> E["初筛 / 出题 / 成本:按 cycle_id 聚合"]
    F --> R["各域口径函数(纯函数)→ Raw*"]
    A --> R
    E --> R
    C -.某个源不可用.-> D["该域标 unavailable + 原因<br/>其余域照常"]
    R --> G["guard.suppress:小格子抑制<br/>(原始聚合进快照的唯一出口)"]
    G --> S["save_snapshot<br/>内容与最新一版相同 → 不落新版本"]
    S --> SNAP[("cycle_metrics_snapshot<br/>只追加,长期保留")]
    SNAP --> RD["GET …/snapshot?baseline_cycle_id=<br/>compare.with_baseline 现算差值"]
```

- **数字全部由代码算**:这条链路没有模型调用。指标目录(`retrospective/catalog.CATALOG`)给每个
  指标稳定 id、名字与口径;口径一改就升 `METRICS_SCHEMA`,不同版本的快照拒绝对比(409)。
- **只进聚合数**:原始聚合只能经 `guard.suppress` 变成快照里的值——计数小于 5(含 0)、
  能从比率或合计反推出小计数的格子都被抑制;对人的判定结果与各部门的格子固定出(缺席不等于 0),
  流程状态桶为 0 时不出格子;分布的键只放行标识符与部门白名单。
- **为什么是快照而不是现查**:对话日志与检索日志随会话删除而消失,周期结束时不留一份,
  这一届就没有基线。快照表不挂在任何清理路径上,存储模块也不提供删除或改写。
- **失败语义**:单个数据源不可用只让用到它的域缺席并在快照里标注(只给类别与异常类名);
  口径函数里的程序错误不算「数据源不可用」,照常抛出。
- 设计与取舍见 `docs/proposals/cycle-retrospective.md`。

报告生成接在快照之后(`POST /admin/retrospective/run`,后台任务):

```mermaid
flowchart TD
    RUN["POST …/run(evaluation:run)"] --> SNAP2["采一次快照(可附基线差值)"]
    SNAP2 --> RES["report_store.start_report<br/>占一个版本:generating"]
    RES -->|Send ×3| AN["analyze:三位分析员并行<br/>各自只拿到自己域的指标视图"]
    AN --> SCR["roles.screen_analysis<br/>依据 / 数字 / 文字三道闸门,逐条丢弃"]
    SCR --> WR["write:撰稿人<br/>只看发现与指标"]
    WR --> GRD{"ground:数字落地守卫"}
    GRD -->|有对不上的数,且没回炉过| WR
    GRD --> ASM["assemble:删掉对不上的句子<br/>记入 unverified"]
    ASM --> MD["render:标题/依据/指标表由代码生成"]
    MD --> DRAFT["finish_report:draft"]
    AN -.分析员失败.-> DEG1["该节只列指标"]
    WR -.撰稿人失败.-> DEG2["各节直接列发现,标「未汇总」"]
    DRAFT --> PUB["POST …/publish:先审计,再 published<br/>旧的已发布版 → superseded"]
```

- **模型只解读**:四个模型角色(prompt 在 `prompts/retrospective/`),交回来的每一条都要带指标 id、
  数字要在引用的指标里对得上、文字不含个人信息与指令样文本;不合格的逐条丢弃并留痕。
- **数字落地守卫**(`retrospective/grounding.py`)比对的是「这句话引用的指标」而不是整份快照;
  模糊量词与中文数字的人数一律按对不上处理。
- **任何一步失败都不让整份报告失败**:分析员失败 → 该节只列指标;撰稿人失败 → 直接列发现;
  生成任务本身出错 → 该版本 `failed`,不影响已有版本。
- **草稿只给有执行权的人看**;只有查看权的人只看得到已发布的版本。
- 组织记忆(勾选教训、召回、到期)还是设计稿,没有实现。

## 文档地图

- 产品设计与决策背景:`docs/design.md` + `docs/adr/`
- 工程规范(分层/错误/日志/注释):`docs/standards/`
- 重构计划与进度:`docs/refactor-roadmap.md`
- 评测可观测 runbook:`docs/eval-observability.md`
