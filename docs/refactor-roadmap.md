# 重构路线图(分期执行,本文为唯一基准)

> **这是什么**:2026-09 代码体检的结论与分期重构计划。之后的重构一律以本文为基准:
> 开工前读对应 chunk,完成后更新 §6 进度追踪。
> **判据来源**:`docs/standards/`(分层、原则、错误处理、日志、注释)。
> **基线说明**:文中 `file:line` 是体检时的快照,重构推进后会漂移——定位以**符号名**为准。

---

## 0. 现状问题总账

### P0 正确性 bug(先于一切结构改动)

| # | 问题 | 位置(基线) |
|---|---|---|
| P0-1 | async 路由直调同步 psycopg,阻塞事件循环;`_log_conversation` 用 `async def` 包同步调用伪装非阻塞,且 `asyncio.create_task` 不存引用 | `web/routes.py`:`_log_conversation`、`list_my_sessions`、`get_admin_sessions`、`delete_my_session`、`get_my_session`、两处 `resolve_thread` 调用点 |
| P0-2 | 静默吞异常零日志:建档降级、建表自举降级、评估仓线失败,失败无声消失 | `routes.py` `_get_or_create_session` 建档 except;`web/app.py` lifespan 建表 except;`evaluation/bundle.py` 仓线循环 except |
| P0-3 | 异常文本直通客户端(`detail=f"...{exc}"`),违反稳定文案纪律 | `web/kb_admin.py`、`web/evaluation_admin.py` 共 3 处 |
| P0-4 | validator 重试循环把一切异常(含程序 bug)当"模型输出不合规"回灌给 LLM;正确写法参照同库评分子图(窄 `except ValueError`) | `evaluation/investigate_graph.py` `generate_node` 校验回灌处 |
| P0-5 | token usage 提取两套口径,评分子图 cache 命中列恒丢(以 `explore.py` 注释的语义为准:raw token_usage 才带 cache 列) | `evaluation/graph.py` vs `evaluation/explore.py` |
| P0-6 | 死代码/失效防线:`_SORT_EPOCH` 零使用;`deep = bool(deep)` 无操作;模块级 `assert` 自检被 `python -O` 剥离;丢弃 `Dossier.add` 返回值改用子串匹配重推 | `routes.py`、`investigate_graph.py`、`explore.py` 对应符号 |

另发现(体检时顺手抓到):`CLAUDE.md` 构建区的 eval 命令 `uv run python -m evals` 实际不可执行
(无 `__main__.py`),真实入口是 `evals/run_evals.py`——已在 Batch 1 修正。

### P1 结构问题(分层 / 耦合 / 可拓展 / 可复用)

- **God module**:`web/routes.py` 约 1200 行,路由之外压着五块独立逻辑——会话注册表与
  TTL/LRU 淘汰、删除中协调(删除期间续聊返 409)、配置指纹与 agent 热重建、对话遥测落账、
  运营配置管理面。同文件混居"用户聊天/运营观测/配置管理"三个 API 域。
- **超长函数**:9 个 >100 行。最重:`_stream_turn`(约 270 行、7 种职责、嵌套最深 8 层)、
  `evaluation/bundle.py` `run_bundle`(约 250 行、7 件事)、`evaluation/runner.py` `_execute_job`
  (约 220 行、单个 160 行 try)、`explore_repo`(约 170 行)、`generate_node`、`adopt_scorecard`、
  `llm_score`、`ensure_kb_schema`、`cli._chat`。
- **God module 二号**:`state/evaluation.py` 约 690 行混 5 类职责(DDL 自举 / scorecard CRUD /
  job 状态机 / requeue 策略 / 管理面投影);每函数新建 PG 连接无池化;bootstrap 守卫样板在
  13 个函数重复。
- **跨模块私有耦合**:`routes._authenticate` 被 2 个管理模块 import;`evaluation/graph._extract_json`
  被 4 个评估模块 import。下划线私有被当公共接口用。
- **重复(同一知识多个出处)**:权限依赖函数 5 份复制品(通用工厂 `_require_any` 已存在却未用);
  `_prompt_version` 4 处;LLM content 解包 4 处内联(`tech_stack._content_text` 已有未用);
  corrective 重试循环 2 份;评分卡版本解析逐字 2 份;会话列表两端点近乎整体复制;requeue
  两条 40 行 SQL 双份;7 个"供测试 patch"的 lazy 转发函数。
- **常量失守**:`SCORING_TEMPERATURE` 同名不同值(0.1 / 0.2 / 内联 0.2);stale 阈值 10 分钟
  写 3 处;错误截断 300/500 两套;explore 降级文案硬编码秒数(改常量文案会撒谎)。
- **prompt 两套并存**(违反 ADR-0004):corrective 提示词、探索触发消息、兜底题面内联在 .py。

### P2 纪律问题(注释 / 日志 / 错误处理)

- **上下文泄漏**:注释与文案中的 issue 号、迭代代号、轮次代号共 src 22 处 + tests 若干
  (自查命令见 `standards/comments.md`);判据:读者能否仅凭本仓查到该标识。
- **错误处理三种纪律并存**:有 noqa+理由+日志(规范样本)、有 noqa 无理由、有纯裸吞。
- **日志**:无 request_id/thread_id 贯穿,未达 `standards/observability.md` 四律。
- **错误分类靠异常文案子串匹配**(认证失败提示词列表),后端文案一改即失效。

---

## 1. 总目标(可验收的终态)

1. **依赖单向**:`web(接口) → graphs(编排) → tools/state(基础设施)`;路由函数只做
   "校验 → 调用 → 组装"(standards/layering.md 反模式清单逐条过)。
2. **核心逻辑无 I/O 可单测**:会话淘汰、删除协调、配置指纹、bundle 各证据线,不启
   FastAPI、不连 PG 即可 pytest。
3. **错误处理一种纪律**:每个 catch 是 log 或 rethrow 二选一;对外错误形状一处定义;
   异常文本零直通。
4. **日志四律落地**:结构化、分级语义正确、request_id/thread_id 从入口贯穿到外部调用。
5. **重复知识单一出处**:权限、重试、usage 提取、prompt 版本、温度常量各只有一个权威出处。
6. **注释零泄漏;prompt 全部走 `prompts/`**(ADR-0004)。

## 2. 设计方式(用什么;明确不用什么)

- **用**:四层分层(主手法);Fowler 重构手法(Extract Class / Move Method / Extract Function,
  纯搬迁不改行为);时钟作参数注入(可测性根基);Pydantic 出入参(data-models 三分法);
  Repository 语义命名 state store;已有的 `_require_any` 工厂用起来。
- **端口适配器仅限既有边界**:LLM 调用与后端 REST client(`tools/client.py`)已有统一边界,保持。
- **明确不用**:GoF 模式点缀;DDD 全家桶;为 store 预建接口/Protocol——没有第二实现或
  无 I/O 单测需求就不抽端口(principles.md 裁决次序:KISS → DRY → SOLID)。

## 3. 测试与安全网(每个 chunk 的开工闸门)

### 3.1 现有测试面

| 层 | 命令 | 说明 |
|---|---|---|
| 机械 | `uv run ruff check .` | 全量,秒级 |
| 单测 | `uv run pytest` | mock/fake,不碰真 IO,默认闸 |
| 集成 | 先起 pgvector 容器,再 `uv run pytest tests/test_kb_integration.py tests/test_evaluation_state_pg.py tests/test_qbank_state_pg.py tests/test_state_pg.py` | 无容器时自 SKIP——**自 SKIP 不算通过**,见 3.3 |
| eval | `uv run python evals/run_evals.py`(可加 `--baseline` 对比) | 需模型 API key;退出码 0 过 / 1 FAIL / 2 全 SKIP |
| E2E | 无浏览器级 E2E | 现状最接近的是 TestClient 全栈路由测试(`test_web_routes.py` 等);真端到端冒烟是空白,Batch 2 动主链路前先补(见 chunk 2.0) |

### 3.2 闸门规则(每个 chunk 都一样)

1. **开工前跑基线**:单测必跑;chunk 涉及 state/kb → 集成档必跑(容器起好);
   涉及 prompt / 模型路由 / 温度 / 图结构 → eval 必跑。**基线红了先修基线,不叠加重构。**
2. **合入前重跑同一组闸门**:全绿 + 行为测试零差异才算完成。
3. 环境 limited(无 API key)时:eval 至少跑到"非 SKIP 的 suite 全过";全 SKIP(退出码 2)
   如实在 PR 里注明,不冒充通过。
4. 每个 chunk 一个 PR(或一组 commit),描述里引用 chunk 编号并列出"本次过了哪些闸门"。

### 3.3 红线

- 集成档**自 SKIP 不是通过**:涉及 DB 的 chunk 必须实际起容器跑过,PR 注明容器方式。
- 动 SSE 主链路(`_stream_turn`)前,chunk 2.0 的行为特征测试必须先落。

### 3.4 基线快照(本文定稿时,重构起点)

- `uv run ruff check .`:全绿。
- `uv run pytest`:**693 passed / 33 skipped**(skip 全部为集成档缺 pgvector 容器的自 SKIP),
  2 条无害 warning(`RunnableConfig` 类型标注)。之后任何 chunk 的基线都必须不低于此。

---

## 4. 批次计划

> 顺序有讲究:Batch 1 是小 diff 修 bug(即使后面停摆,收益已落袋)→ Batch 2/3 结构搬迁
> → Batch 4 横切收尾。每批内的 chunk 尽量独立可并行;有依赖的已在 chunk 里标注。

### Batch 1 — 正确性修复(不动结构)

**chunk 1.1 修复异步阻塞(P0-1)**
- 范围:`routes.py` 全部 async 路由里的同步 store 调用;`_log_conversation`。
- 做法:统一 `await asyncio.to_thread(...)` 包裹(对齐 `evaluation_admin.py`/`kb_admin.py`
  既有纪律);`_log_conversation` 的后台任务保存引用(挂到 `asyncio.Task` 集合,done 后discard)。
- 验收:grep 确认 async def 内无直调同步 psycopg 函数;web 路由测试全绿。

**chunk 1.2 静默吞异常补日志(P0-2)**
- 做法:三处降级 `except` 补 `logger.warning(..., exc_info=True)`;**保留 fail-open 语义不变**,
  注释已写为什么吞,现在补"留痕"。
- 验收:三条失败路径在测试中可观测到 warning;降级行为零变化。

**chunk 1.3 管理面错误文案统一(P0-3,执行中精化了口径)**
- 精化:按 standards/error-handling.md 三分类,**受控领域文案是对外契约**
  (KbValidationError/EmbeddingError/LookError 422、runner 归属错位 RuntimeError——
  消息是面向管理面的错因,含 resume_id 等业务字段,保留并注明);真正泄漏的是
  **未预期异常的原文**(可能含 SQL/路径)。
- 做法:kb_admin 兜底分支 `detail=f"...{exc}"` → 稳定文案 + logger.warning(exc_info);
  两处领域文案站点加契约注释;共享文案 helper 推迟到 Batch 2(拆 admin router 时
  随模块归位,现在抽是提前抽象)。
- 验收:grep 确认未预期异常原文不再进任何响应体;kb_admin 兜底分支有测试断言
  (稳定文案 + 原始异常不出现)。

**chunk 1.4 异常分类与 usage 口径收口(P0-4、P0-5)**
- 做法:investigate 校验回灌的 except 收窄为 `except ValueError`(对齐评分子图写法);
  usage 提取合并为单处实现(兼并 `routes.py` 的 `extract_usage` 转发与 explore/graph 两套口径,
  保留 cache 列)。
- 验收:程序异常不再回灌给模型(单测:抛 KeyError 时走失败分支而非重试回灌);两处 usage
  行为一致(cache 列有值)。

**chunk 1.5 死代码与失效防线清理(P0-6)**
- 做法:删 `_SORT_EPOCH`;删 `deep = bool(deep)`;模块级 `assert` 自检改 `if ...: raise RuntimeError`;
  dossier 写入判定改用 `Dossier.add` 返回值。
- 验收:相关单测绿;grep 无残留。

**chunk 1.6 eval 命令文档修正(顺手)**
- 做法:`CLAUDE.md` 构建区 eval 命令更正为 `uv run python evals/run_evals.py`;与
  `evals/README.md` 口径对齐。
- 验收:照文档命令实跑能进入 runner(全 SKIP 也算入口通)。

### Batch 2 — web 层拆分(routes.py 归位)

**chunk 2.0 主链路行为特征测试(前置,动结构前必落)**
- 做法:TestClient 级 SSE 冒烟——一次对话回合的**事件序列**断言(delta/tool 事件/收尾),
  会话创建→恢复→删除 409→淘汰的全生命周期表驱动用例;缺的补进 `tests/test_web_routes.py` /
  `test_sessions_routes.py`。
- 验收:Batch 2 全程这组测试不许改断言(只许随搬迁改 import)。

**chunk 2.1 共享鉴权模块 `web/auth.py`**
- 做法:`_authenticate` 与 `_require_any` 工厂上移;routes / evaluation_admin / kb_admin 的
  5 份权限复制品全删,私有跨模块 import 清零。
- 验收:权限依赖全库单处定义;`grep "from official_agent.web.routes import _"` 零命中。

**chunk 2.2 会话存储抽取 `web/session_store.py`**(依赖 2.0)
- 做法:`_sessions` / `_deleting_sessions` / 淘汰算法 / 删除协调 → 纯 Python 类;
  时钟(`now`)作参数注入;FastAPI 零依赖。
- 验收:注册表不 import FastAPI;TTL 淘汰、LRU 上限、删除中 409 有独立单测。

**chunk 2.3 配置热重建抽取**
- 做法:`_config_fingerprint` / `_ensure_fresh_agent_config` → 独立模块;指纹获取失败从
  静默降级改为 warning(衔接 chunk 1.2 纪律)。
- 验收:`test_config_hot_reload.py` 随迁全绿;重建/不重建两分支有单测。

**chunk 2.4 遥测落账抽取**
- 做法:`_log_conversation` / `_collect_sources` / `prefix_hash` → conversation_log 模块
  (含 chunk 1.1 的 to_thread 修复成果一起搬)。
- 验收:routes.py 不再含落账细节;落账失败留痕有测试。

**chunk 2.5 运营配置管理面拆 router**
- 做法:配置白名单校验、secret 掩码、base_url 安全校验 → `web/config_admin.py`;
  `test_admin_config.py` 随迁。
- 验收:routes.py 只剩对话与会话路由;admin 面测试全绿。

**chunk 2.6 `_stream_turn` 拆解**(依赖 2.0、2.2)
- 做法:按天然分界拆四块——流消费(messages/updates 双模式)、usage 跨 chunk 去重累计、
  toolless 编造守卫段、落账组装;嵌套 8 层改 guard clause。
- 验收:chunk 2.0 事件序列断言零差异;每块职责单一(嵌套 ≤3 层,长度以"不用滚动分段注释
  能读"为准,standards/readability.md)。

**chunk 2.7 chat 入参 Pydantic 化**
- 做法:裸 `request.json()` → Pydantic 模型(校验在边界,standards/data-models.md)。
- 验收:畸形 JSON / 数组 body 返 400 而非 500,测试覆盖。

**Batch 2 总验收**:routes.py 降到约 400 行以内;web 包 import 图无环;§3.2 闸门全绿。

### Batch 3 — evaluation / state 层

**chunk 3.1 `state/evaluation.py` 拆分 + 连接池**
- 做法:拆 `scorecard_store` / `job_store`(含 requeue 策略)/ `review_queue` 投影 /
  共享 bootstrap 模块(收掉 13 处守卫样板);`_conn()` 换 `psycopg_pool`;requeue 双份
  SQL 合并(cycle 过滤参数化)。
- 验收:各 store 可 mock 连接独立单测;集成档真容器全绿;连接复用可观测(单测断言池命中)。

**chunk 3.2 评估大函数拆解**
- 做法:`run_bundle` 每条证据线(仓/评测错因/奖项/兜底)各拆一个协程,v2 信封展平拆纯函数;
  `_execute_job` 拆 `_do_scoring` / `_do_qbank`;`explore_repo` 拆出工具执行内循环;
  `route_node` 归因字典字面量三份 → 一个 helper。
- 验收:每条线可独立单测;bundle/runner/explore/investigation 现有测试零差异。

**chunk 3.3 重复与常量收口**
- 做法:公共 `invoke_with_retry(model, prompt, validator)` 统一 corrective 重试(含防注入
  包装,注释只留一份);`_content_text` / `_prompt_version` 移入评估公共模块;
  `SCORING_TEMPERATURE`、stale 分钟数、错误截断长度全部具名并单一出处;explore 降级
  文案不硬编码数字(引用常量)。
- 验收:对 §0 P1"重复"清单逐项 grep 复查为单处;相关测试全绿。

### Batch 4 — 横切收尾

**chunk 4.1 上下文泄漏改写**
- 做法:按 `standards/comments.md` 自查 grep 全库扫描(src、tests、docs、CLAUDE.md),
  逐条把编号背后的知识写进正文(不是删号了事);仓库内稳定引用(ADR、docs 路径)
  保留并写清所指。
- 验收:自查 grep 零命中(规范文档中的反例引用除外)。

**chunk 4.2 日志四律落地**
- 做法:web 入口中间件生成 request_id 并透传;各层按 `standards/observability.md`
  "各层记什么"表补齐;ERROR 必带堆栈+上下文;敏感信息红线核查(简历 PII 重点)。
- 验收:一次对话从 web 到 tools/state 的日志能按 request_id 串成一条线(手工核 + 现有
  trace 传播测试扩展)。

**chunk 4.3 统一错误模型**
- 做法:Pydantic 错误响应模型一处定义;`_AUTH_FAIL_HINTS` 文案子串匹配改为异常类型判定
  (自定义异常层级:业务错误 / 系统错误,standards/error-handling.md 三分类)。
- 验收:全库错误响应形状一致(测试断言);分类不再依赖文案。

### Batch 5 — 文档收尾(重构完成后执行;给后续开发者的交付物)

> 重构不改文档 = 欠账(双轨纪律:CLAUDE.md 管 agent 轨,docs/ 管人读轨,同一事实
> 一处全文一处指针)。本批把重构期间的变化全部回写文档,仓库才能交给新人。

**chunk 5.1 技术文档:架构与模块导览**
- 做法:重构后更新 `docs/design.md` 与 `docs/standards/README.md` 落位表——目录导览
  (web/graphs/tools/state/kb/evaluation 各模块一句话职责)、依赖方向图(四层骨架映射)、
  两条关键流程 walkthrough(一次对话回合:web→graphs→tools/state;一个评估 job:
  runner→bundle→state)。README 命令表与 CLAUDE.md 构建区核对到与实际一致。
- 验收:新人按文档能跑起测试、能定位任一功能的所属模块;文档模块清单与代码一一对得上。

**chunk 5.2 设计文档:重构决策的 ADR 记录**
- 做法:批次级结构取舍各记一篇 ADR(工作区 `../docs/adr/`),至少覆盖——web 层拆分
  (会话存储/配置热重建的边界与归属)、state store 拆分与连接池引入、统一错误模型与
  异常层级、request_id 贯穿方案;重构波及的既有 ADR 如语义变化则出新版本,不原地重写。
- 验收:Batch 2/3 的每个结构决策都能在 ADR 里找到「为什么这么拆、备选是什么」;
  可逆的小决策留在 commit message,不堆 ADR(docs-sync 判据)。

**chunk 5.3 开发者上手指南**
- 做法:`docs/standards/README.md` 增加「上手路径」——必读顺序(CLAUDE.md →
  standards → design.md)、本地起服务与测试命令、集成档容器与 eval 的运行方式、
  分支/提交约定。
- 验收:照指南从 clone 到提交一个最小改动全程无歧义(实际演练一遍)。

---

## 5. 防跑偏红线

1. **结构搬迁与行为修改永不同 commit**;每步全量单测绿了才下一步。
2. **不预建抽象**:拆出的模块不设接口/Protocol,除非出现第二实现或无 I/O 单测需求。
3. 每 chunk 合入前过一遍 `standards/comments.md` 的机械自查,新泄漏不许进库。
4. 一 chunk 一 PR,范围不蔓延;顺手发现的问题记回本文,不当场扩scope。
5. 行号会漂移:定位一律用符号名;发现本文与代码不符时,以代码为准并回改本文。

## 6. 进度追踪(完成一个 chunk 更新一格)

| Chunk | 内容 | 状态 | 备注 |
|---|---|---|---|
| 1.1 | 异步阻塞修复 | 待办 | |
| 1.2 | 吞异常补日志 | 待办 | |
| 1.3 | 管理面错误文案 | 待办 | |
| 1.4 | 异常分类 + usage 口径 | 待办 | |
| 1.5 | 死代码清理 | 待办 | |
| 1.6 | eval 命令文档修正 | 待办 | |
| 2.0 | 主链路特征测试 | 待办 | Batch 2 前置 |
| 2.1 | 共享鉴权模块 | 待办 | |
| 2.2 | 会话存储抽取 | 待办 | 依赖 2.0 |
| 2.3 | 配置热重建抽取 | 待办 | |
| 2.4 | 遥测落账抽取 | 待办 | |
| 2.5 | 配置管理面拆分 | 待办 | |
| 2.6 | _stream_turn 拆解 | 待办 | 依赖 2.0、2.2 |
| 2.7 | chat 入参 Pydantic 化 | 待办 | |
| 3.1 | state/evaluation 拆分 | 待办 | |
| 3.2 | 评估大函数拆解 | 待办 | |
| 3.3 | 重复与常量收口 | 待办 | |
| 4.1 | 上下文泄漏改写 | 待办 | 范围含 docs 与 CLAUDE.md |
| 4.2 | 日志四律落地 | 待办 | |
| 4.3 | 统一错误模型 | 待办 | |
| 5.1 | 技术文档:架构与模块导览 | 待办 | Batch 2-4 完成后 |
| 5.2 | 设计文档:重构决策 ADR | 待办 | Batch 2-4 完成后 |
| 5.3 | 开发者上手指南 | 待办 | 依赖 5.1 |
