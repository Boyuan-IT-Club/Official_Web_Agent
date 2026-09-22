# 工程规范(开发必读)

> 本目录是本仓的工程规范细则,面向所有开发者(人及 AI 协作者)。
> 仓根 `CLAUDE.md` 与工作区根 `CLAUDE.md` 是红线与指针(必读);本目录是它们背后的细则与判据。
> 每份文档开头标了「何时读」,按环节取用,不必整包读完。

## 上手路径(第一周,从 clone 到第一个 PR)

1. **必读顺序**:仓根 `CLAUDE.md`(红线/命令)→ 本目录 → `docs/design.md`(架构与
   代码导览)→ 涉及的 ADR(`../docs/adr/`,工作区根)。
2. **本地起环境**:`uv sync` 装依赖(含 dev 组);`uv run official-agent --help` 确认 CLI 可用。
3. **日常闸门**:`uv run ruff check .` + `uv run pytest`(全量单测,不碰真 IO)。
4. **集成档**(改 `state/`、`kb/` 或 SQL 时必须真跑):
   ```bash
   docker compose -f deploy/docker-compose.local.yml up -d agent-pg   # pgvector,端口 5433
   POSTGRES_URL=postgresql://postgres:agent_dev@localhost:5433/official_agent \
     uv run pytest tests/test_evaluation_state_pg.py tests/test_qbank_state_pg.py \
                   tests/test_state_pg.py tests/test_kb_integration.py
   ```
   (自 SKIP 不是通过;`.env` 已含同样的 POSTGRES_URL。)
5. **eval 门禁**(改 prompt/模型路由/温度/图结构时必跑):
   `uv run python evals/run_evals.py --help` 看参数;需模型 API key,基线对比防回归。
6. **分支与提交**:分支 `feat/` `fix/` `refactor/` `docs/` 前缀;Conventional Commits
   中文 subject(`<type>(<scope>): <做什么>`),body 写为什么;提交信息引用 issue 编号。
7. **最小改动闭环**:建分支 → 改 → 闸门全绿 → 提交。规范依据看本目录加载地图;
   注释红线与自查 grep 见 [comments.md](comments.md)。

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

## 四层骨架在本仓的落位(2026-09 结构化重构后)

| 规范层 | 本仓目录 | 说明 |
|---|---|---|
| 接口层 | `src/official_agent/web/` | FastAPI 路由与入口横切:routes(chat/会话)、各 admin 面、auth、session_store、agent_factory、telemetry、app |
| 应用/编排层 | `src/official_agent/graphs/`、`evals/` | LangGraph 图装配与编排、eval runner |
| 评估域 | `src/official_agent/evaluation/` | 流水线领域逻辑:runner/bundle/explore/graph/judge/tech_stack/llm_common 等 |
| 基础设施层 | `src/official_agent/tools/`、`state/`、`kb/`、`security/` | 后端 REST 客户端、Postgres 状态面(evaluation 为包)、pgvector 检索、PII/注入守卫 |

依赖**单向向下**:`web → graphs → evaluation → tools/state/kb/security`;
领域规则不 import 基础设施实现细节。模块逐个职责见 `docs/design.md` §4 代码导览。

## 维护原则

- 每条规则写「为什么」与来源(各文末尾来源节);改规范先过 ADR 或在 PR 里说明动机。
- 硬数字(行数/缩进/覆盖率)不写死在这里——机械规则交 ruff/CI 配置,这里只留原则与判据。
