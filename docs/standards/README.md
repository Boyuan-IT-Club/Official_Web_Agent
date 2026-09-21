# 工程规范(开发必读)

> 本目录是本仓的工程规范细则,面向所有开发者(人及 AI 协作者)。
> 仓根 `CLAUDE.md` 与工作区根 `CLAUDE.md` 是红线与指针(必读);本目录是它们背后的细则与判据。
> 每份文档开头标了「何时读」,按环节取用,不必整包读完。

## 加载地图(按环节触发,MUST = 该环节开工前必读)

| 触发(做什么时) | MUST 读 | 按需读 |
|---|---|---|
| **写代码 / 重构** | [comments.md](comments.md) · [readability.md](readability.md) | [python.md](python.md)(本仓语言惯用法)、[error-handling.md](error-handling.md)(写错误路径/外部调用时)、[observability.md](observability.md)(加日志时)、[principles.md](principles.md)(纠结要不要抽层/接口时) |
| **设计新模块 / 评审分层与接口** | [principles.md](principles.md) · [layering.md](layering.md) | [data-models.md](data-models.md)(定 entity/request/response、写 mapper 时) |

## 文档清单

- `principles.md` — SOLID/DRY/KISS 的裁决次序与应用判据、高内聚低耦合
- `layering.md` — 默认四层骨架、依赖规则(端口在 consumer 侧)、YAGNI 加层信号、反模式
- `data-models.md` — entity/request/response 三分法、DO/DTO/BO/VO 引入信号、Repository vs DAO、Mapper 规范
- `observability.md` — 日志四律、各层记什么、request_id 串联、三支柱起步最小集
- `comments.md` — 五级注释决策表、接口注释优先、泄露红线(含机械自查 grep)
- `readability.md` — 命名、函数
- `error-handling.md` — 错误三类、分层传播、外部调用(超时/重试三问)、禁兜底红线
- `python.md` — Python 独有惯用法、易踩坑(含异步陷阱)、LangGraph 要点

## 四层骨架在本仓的落位(近似对应)

| 规范层 | 本仓目录 | 说明 |
|---|---|---|
| 接口层 | `src/official_agent/web/` | FastAPI 路由:鉴权、SSE 流式、出入参校验 |
| 应用/编排层 | `src/official_agent/graphs/`、`evals/` | LangGraph 图装配与编排、eval runner |
| 基础设施层 | `src/official_agent/tools/`、`state/`、`kb/`、`security/` | 后端 REST 客户端、Postgres 状态存储、pgvector 检索、PII/注入守卫 |
| 领域规则 | (收敛中) | 目前部分规则仍散在 web/graphs 中,新增规则请按 [layering.md](layering.md) 归位,别再往 route 里堆 |

依赖应**单向向下**:`web → graphs → tools/state`;领域规则不 import 基础设施实现细节。

## 维护原则

- 每条规则写「为什么」与来源(各文末尾来源节);改规范先过 ADR 或在 PR 里说明动机。
- 硬数字(行数/缩进/覆盖率)不写死在这里——机械规则交 ruff/CI 配置,这里只留原则与判据。
