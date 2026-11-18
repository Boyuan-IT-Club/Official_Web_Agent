# 提案:官网对话助理的逐轮意图提示

> 状态:2026-11-18 起草并实现机制。**规则判断、提示模板、三档开关、对话日志记录、确定性门禁、
> 多轮对照评测的执行器与对照脚本已实现,开关缺省 `off`**。真实模型下的 off / hint 对照、
> Agent 与 Java 测试环境的联调、启用决策都**还没做**——本文里凡是「目标」「门槛」都是提案,
> 不是已测得的成绩。基准代码为主分支 `a9ed36f`。
>
> 与 ADR-0003 的关系:ADR-0003 砍掉了意图分类节点,只留下「若 badcase 证明需要,只作软路由
> 提示」的位置。本提案就是这个位置的落地:不做硬闸、不预裁剪工具,只往 system prompt 末尾追加
> 一段固定模板的参考;是否启用由 §8 的对照评测决定。与 ADR 冲突时以 ADR 为准。

## 1. 目标与范围

用一个月完成一项可以演示、测量和复盘的改进。切入点是「用户是在问改期规则、查空位,还是要求
实际改期」:给官网对话助理加逐轮意图识别,让同一个场景既能说明 Agent 怎么选工具,也能说明
Java 后端如何决定一笔业务操作能否生效。

**在范围内**:官网 `POST /chat` 这一个入口;规则判断与提示;记录与对照评测;与 Java 测试环境的
一条面试安排流程联调。

**不在范围内**:后台简历批处理(B 模块)和面试官页面(C 模块)各有入口,不动;`graphs/router.py`
的循环节点仍是占位,改那里覆盖不到官网聊天;不新增写工具;不改装配层与确认令牌。

## 2. 先做哪几件事

| 优先级 | 改进 | 四周内的交付物 | 现状 |
|---|---|---|---|
| P0 | 建立工具选择与业务结果的对照基线 | 固定语料、可重复的离线评测、现有版本的错例清单 | 语料与执行器已就绪(§7);**真实模型基线未跑** |
| P0 | 在官网聊天加入本轮意图提示 | 配置开关、只记录预测的模式、接入对话的版本、多轮和确认流程回归 | **已实现**(§4–§6),缺省 off |
| P0 | 联调一条完整的面试安排流程 | Agent 与 Java 测试环境的操作记录,含成功、用户取消、后端拒绝和结果未知四种情况 | 操作手册已写(§9);**未执行** |
| P1 | 把确定性回归接到 PR 检查 | 每个 PR 验证权限范围、缺参、确认和执行边界;真实模型对照报告单独保存 | 意图门禁与 Web 回归已进 pytest(CI 现有 `uv run pytest` 即覆盖);对照报告的存放约定见 §7.3 |
| 后续 | 按实测失败原因扩展 | 知识问答错得多就调检索;初筛质量有独立需求时,再评估不同评分模式 | 见 §13 |

## 3. 标签、标记与证据

原则:**「想办什么」与「当前能不能办」分开记录**。标签只看本轮文本和会话里的结构化选项,与
身份无关——候选人和管理员都说「帮我把面试改到周六」,标签相同;能调用的工具由装配层按身份决定,
身份只影响提示文本里点名哪些工具。分数可见性、身份、权限码和确认状态始终由现有业务逻辑判断,
意图模块不复判。

### 3.1 标签(六个,互斥)

| 标签 | 含义 | 典型问法 | 边界 |
|---|---|---|---|
| `rules_inquiry` 规则咨询 | 问招新规则、流程、社团公开信息 | 「改期要走什么流程」「技术部做什么」 | 只有「怎么/流程/规则」而没有带去向的动作 |
| `self_lookup` 本人查询 | 查用户本人的安排/进度 | 「我的面试改到几点了」「我被排到哪了」 | 「改到几点了」这类问句不算写 |
| `admin_lookup` 管理查询 | 查招新数据:场次余量、改期申请列表、未分配、统计、简历 | 「周六下午还有空位吗」「有哪些待处理的改期申请」 | 「待处理」是状态不是动作 |
| `schedule_interview` 安排面试 | 要求把某人排到/调到某场次 | 「把 11 号简历排到周六」「帮我把面试改到周六」 | 查场次是它的前置步骤,同一分句里出现「空位」不算复合 |
| `handle_reschedule` 审批改期 | 同意/拒绝某条改期申请 | 「同意 27 号改期申请」 | 同意只取消原安排,排新场次是后续独立操作 |
| `other` 其他 | 寒暄、放弃、只有指代、问分数、判不准 | 「你好」「算了」「看看那个」 | 判不准时 `uncertain=true` |

### 3.2 标记(可叠加)

| 标记 | 何时打 | 提示里怎么说 |
|---|---|---|
| `negation` | 分句开头是放弃/改口(「算了」「先别改」「不排了」);裸「不」必须跟动作动词 | 本轮不要发起写操作,除非本条消息重新明确要求 |
| `prior_reference` | 「第二个」「最后一个」「那个」 | 选项能解析时写出选中项;不能解析时转成下面两个标记之一 |
| `missing_target` | 写请求没有简历号/申请号且不是「我的面试」;或指代找不到选项 / 序号越界 | 先问一个能决定下一步的问题,不要猜 ID |
| `multiple_targets` | 写请求里有两个以上 ID;或上一轮有多个选项而本轮只说「那个」 | 先确认是哪一个,写操作一次只针对一个对象 |
| `compound` | 两个以上分句各自有归类,且标签或涉及的工具不同 | 逐项处理,写操作一次只发起一项 |
| `restricted_score` | 问到评分、分数、等级 | 不在对话可提供范围,不为此调任何查询工具 |

### 3.3 证据来源类型

`keyword`(命中词表)、`prior_options`(用到了上一轮工具结果里的选项)、`pending_cancelled`
(本轮开始前刚因新消息取消了一张待确认卡片)。记录里只存类型,不存原文。

### 3.4 场景与期望

| 人与问法 | 期望识别 | 助理应有的行为 |
|---|---|---|
| 候选人「面试改期要走什么流程?」 | `rules_inquiry` | 检索本期规则,给出来源;没有资料就说明缺口 |
| 候选人「我的面试改到几点了?」 | `self_lookup` | 用本人身份查当前安排;「改」字不代表本轮要写入 |
| 管理员「周六下午还有空位吗?」 | `admin_lookup` | 查开放周期和可用场次,有结果再回答 |
| 管理员「把 11 号简历排到周六下午还有空位的场次」 | `schedule_interview` | 查场次;多个候选先请用户选一个;唯一确定后出确认卡 |
| 候选人「帮我把面试改到周六」 | `schedule_interview` | 候选人没有排期工具:说明可用的申请流程,不能替管理员执行 |
| 管理员「先别改,帮我看看周六」 | `admin_lookup` + `negation` | 若有待确认操作,沿用新消息取消旧操作的行为,然后只查场次 |
| 管理员「同意 27 号改期申请」 | `handle_reschedule` | 确认卡说明同意会取消原安排;安排新场次是后续独立操作 |
| 管理员「那就第二个」(上轮列了场次) | `schedule_interview` + `prior_reference` | 只在上一轮确实列出选项时解析指代;选中场次后仍需确认 |
| 管理员「那就第二个」(上轮没列) | `other` + `prior_reference` + `missing_target` | 追问指的是什么 |
| 用户「分数是多少?」 | `other` + `restricted_score` | 沿用现有对话可见性规则 |
| 用户「看看那个」 | `other` + `prior_reference` + `missing_target` | 只问一个能决定下一步的问题 |
| 管理员「怎么把面试改到周六?」 | `other`,`uncertain` | 问做法还是要执行说不准:不出提示,由原循环推理 |

## 4. 判定规则(`graphs/assistant/intent.py`,规则版本 `intent_rules/v1`)

1. **规范化**:NFKC(全角转半角)、小写、去空白。
2. **分句**:按 `,;。!?` 与「然后/顺便/另外/并且/同时/再帮我」切开;「、」不切(它常连接多个 ID)。
3. **改口**:每个分句开头反复剥掉放弃/改口短语,剥过就打 `negation`,剩余部分再归类。
4. **分句归类**(先中先得):评分词 → `restricted_score`;「改期」+ 审批动词 → `handle_reschedule`;
   带去向的排期动词(排到/安排到/分配到/调到/改到/换到…)且不是「改到几点了」式问句 → 若同句还有
   「怎么/如何/流程/规则/步骤」则**不确定**,否则 `schedule_interview`;本人句式 → `self_lookup`;
   管理数据话题(空位/场次、改期申请、未分配、统计、简历、候选人卡片;或「看看/查一下」+日期)→
   `admin_lookup`;规则词 → `rules_inquiry`。
5. **合并**:任一分句不确定 → 整轮 `other` + `uncertain`,不出提示。否则主标签按「审批改期 > 安排面试
   > 本人查询 > 管理查询 > 规则咨询」取最需要谨慎的一项;建议工具取各分句并集。
6. **指代**:序号(第 N 个/场/条)、「最后一个」、或去掉虚词后只剩「这个/那个」类指示词。只对
   **上一轮**工具结果里的选项解析(场次来自 `find_available_sessions`,申请来自
   `list_reschedule_requests`);更早轮次的选项不算——它们可能已经变了,应重新查。本轮没有其他归类时,
   按选项种类承接成安排面试或审批改期。
7. **目标 ID**(仅写请求、且没有指代时):先去掉日期与时刻,再找「简历/候选人 N」「N 号」「#N」
   「N 和 M」;审批找「N 号申请」「申请 #N」。零个且不是「我的面试」→ `missing_target`;两个以上 →
   `multiple_targets`。

改任何规则或词表都要 bump `INTENT_VERSION`;它随记录落库、随评测报告的 `context` 输出,两次运行
按它区分。**只有观察到规则无法覆盖的一组稳定错例**(同一类问法在对照里反复失败、且改词表会误伤
其他组),才对这些歧义输入试一次轻量分类模型,并单独统计新增调用成本(ADR-0004:新模型角色先有
eval 再上线;降 light 档须 eval 证明)。

## 5. 怎样接入现有代码

### 5.1 时序(`web/routes.py` 的 `_stream_turn`)

```
锁 → 配置热生效 → session 事件 → 模型并发闸 + 墙钟超时
  → [普通新消息] 新消息取消旧确认卡(_cancel_pending_confirmation,返回是否取消)
  → 记忆召回
  → [普通新消息] 意图判断 _turn_intent(读一次会话状态取上一轮选项 → classify_turn → render_hint)
  → astream(context={memories, org_memories, intent_hint})
  → 守卫 / 压缩 → conversation_log 落一行(含 intent 记录) → done
```

- 意图判断放在旧确认状态处理完之后,`pending_cancelled` 证据由此而来。
- 提示经运行时上下文进 `AssistantContext.intent_hint`,由现有动态 prompt 中间件拼在 system prompt
  **最末尾**(记忆段之后):静态正文、身份段、工具契约、记忆段的缓存前缀都不受影响;提示不进
  checkpoint、不进历史回看、不被压缩。
- 耗时:规则判断 + 渲染实测约 8 微秒/轮(60 条用例 × 200 遍取均值,开发机);shadow / hint 档另多
  一次 `aget_state`(读 checkpoint)。耗时计入本轮 `duration_ms`,另在记录里单列 `elapsed_ms`。

### 5.2 三档开关(`config.assistant_intent_mode`,环境变量 `ASSISTANT_INTENT_MODE`)

| 档位 | 计算 | 记录 | 交给模型 | 用途 |
|---|---|---|---|---|
| `off`(缺省) | 否 | 否 | 否 | 基线;与接入前行为一致 |
| `shadow` | 是 | 是 | 否 | 线上只观测预测分布与错例,不影响回答 |
| `hint` | 是 | 是 | 是 | 启用 |

不进 `HOT_KEYS`:切档是对照实验的边界,改了要重启,免得一段日志里混着两种模式。观察模式与实际
启用的两组结果分开统计(按记录里的 `mode` 分组)。

### 5.3 提示模板(`prompts/assistant_intent_hint.md`,版本 `assistant_intent_hint/v1`)

一个「## 键」一段:`header`、六个标签各一段(安排/审批另有「选中了上轮选项」「没有写工具」两种
变体)、`no_tool`、每个标记一段。代码只填 `{tools}` 等占位符,**只填本会话已装配的工具**;
没有对应工具时改用「不能办」的说法。例:管理员问「周六还有空位吗」,提示为

```
# 本轮意图提示
以下是系统按固定规则对用户本轮消息的初步判断,只作参考:……写操作仍然要经过确认卡片。
- 本轮倾向查询招新数据,不是要求执行操作:可优先用 get_open_cycle、find_available_sessions 查询,……
```

上一轮选项写进提示时只取 ID、日期与时刻,并滤成数字与分隔符——地点等自由文本(可能含注入)
不进 system prompt。

### 5.4 记录

`agent_conversation_log.intent`(jsonb,幂等加列),确认续跑、off 档、判断失败的轮次为 NULL:

```json
{"version": "intent_rules/v1", "mode": "hint", "label": "schedule_interview",
 "flags": ["prior_reference"], "evidence": ["prior_options"],
 "tools": ["get_open_cycle", "find_available_sessions", "assign_interview"],
 "uncertain": false, "chosen_option": 6,
 "hinted": true, "hint_prompt": "assistant_intent_hint/v1", "elapsed_ms": 3}
```

只含枚举值与 ID,不含用户原文。实际调用的工具、是否出卡(`tools` 列 / 确认事件)、耗时与 token
已在同一行;trace 侧在 Langfuse metadata 里带 `intent_label` / `intent_mode` / `intent_version`,
与已有的 `trace_id` 列关联。错误行也保留 `intent`:对照报告要看失败轮次当时判成了什么。

### 5.5 失败语义

意图提示是观测/增值面,**fail-open**(ADR-0005):读状态或判断抛异常 → 记一条 warning,本轮
`intent_hint=""`、记录为 NULL,原循环照常推理。写路径不受影响——它本来就不经过这里。

## 6. 确认续跑的边界(不变量)

1. `POST /chat/confirm` 直接恢复已挂起的操作,**不做意图判断**(`resume` 非空时跳过)。
2. 聊天里发「好的」「第二个」是普通消息,不等同于点确认:按原逻辑先取消挂起的操作,再判断意图。
3. 分类器不能授予工具、生成批准凭证或重发写请求——它的输出只进 system prompt 的参考段与日志。
4. 操作目标或场次变化时,模型按新参数调用写工具,确认卡按新参数生成新指纹
   (`tools/interrupt_guard.py`);旧指纹在 `/chat/confirm` 被拒(409)。
5. Java 后端根据当时的权限和余量作最终决定;Agent 以写工具返回的 `status`(done / cancelled /
   error / unknown)为准。

钉住这些的测试:`tests/test_web_intent.py`(确认续跑不判断、「好的」走取消而非 Command、三档行为、
fail-open)、`tests/test_web_confirm.py`(指纹与续跑)、`tests/test_interrupt_guard.py`(指纹绑定)。

## 7. 评测

### 7.1 规则门禁 `evals/datasets/intent_probes.yaml`(确定性,进 PR 检查)

- 60 条合成单句:dev 20 条(7 组)用来调规则与模板;holdout 40 条(15 组)只看不调。同一句话的
  改写放在同一个 group,runner 检查 group 不跨 split。覆盖身份差异、明确读写、否定、多轮指代、
  信息不足、复合请求、无写权限的管理员、寒暄。
- 每条断言标签、标记集合(精确相等)、是否不确定、建议工具(子集)、选中第几项;另有一条
  不写在期望里的不变量:**提示只点名该身份已装配的工具**,不确定时不出提示。
- 判定:dev 必须全过;holdout 通过率 ≥ 0.8(写数据集时定下,不随规则调整)。
- **首轮结果**:dev 20/20,holdout 38/40。两条失败同一原因——「有哪些**待处理**的改期申请」里的
  「处理」被当成审批动词。修正为「待处理」不算动作后 holdout 40/40。**这次修正看过 holdout,
  此后 holdout 不再严格独立**;且 60 条都由同一人在写规则前编写,与规则作者同源。真正独立的验收
  要靠 §7.4 补的匿名化真实用语。

```bash
uv run python evals/run_evals.py --suite intent_probes
uv run pytest tests/test_turn_intent.py
```

### 7.2 模型对照 `evals/cases/tool_selection.yaml`(真实模型,不进 PR 检查)

执行器新增:`history`(同一会话先发几轮,断言只看最后一轮)、`sessions`(覆盖假后端场次)、
`--intent-mode`(逐轮用与 web 入口同一套函数算意图并交给图)、每条明细带意图标签、
`observations` 记最后一轮的耗时 P50/P95、平均模型调用数与 token。新增 8 条对照用例:候选人问
改期规则、问句带「改」的本人查询、改口只查、上一轮列三个场次后选第二个、没有选项时说「第二个」、
审批改期、对象不明的分数、复合查询。

```bash
uv run python evals/run_evals.py --suite tool_selection --intent-mode off  --json evals/reports/ts_off.json
uv run python evals/run_evals.py --suite tool_selection --intent-mode hint --json evals/reports/ts_hint.json
uv run python evals/compare_reports.py --before evals/reports/ts_off.json --after evals/reports/ts_hint.json
```

`compare_reports.py` 输出 Markdown:每个 suite 的运行条件、「原错新对 / 原对新错 / 一直通过 /
一直失败 / 不稳定」各几条(分母是两侧都有的用例数)、观测量前后对照。结果波动的样例每侧重复
三轮,把三份报告都传进去,逐条按多数定结论,平票单列为不稳定、不计入任何一类。

现有 `tool_selection` 断言工具集合、参数和禁止调用;它不能单凭自己证明多轮确认和后端写入正确。
分工:规则与固定上下文在 `intent_probes` + pytest;多轮确认在 Web 会话测试;少量真实模型对照用
上面的假后端;跨服务验收用 §9 的 Java 测试环境。

### 7.3 指标口径

| 要回答的问题 | 怎么测 | 落在哪 |
|---|---|---|
| 用户的任务完成得更好吗? | 同一模型版本、同一份假后端数据、同一采样设置下,off 与 hint 各跑一次,逐条成功/失败。需要追问时问对问题、没有请求写入时只返回查询结果,都算成功;猜中标签但办错业务算失败 | `tool_selection` 的 pass/fail;报告 `context` 记 `intent_mode`、`model`、规则与模板版本 |
| 到底修好了哪些错例? | 「原错新对」「原对新错」及分母;按用例 id 前缀看是哪类问法改善;波动样例重复三轮 | `compare_reports.py` |
| 分类代价是否划算? | 端到端 P50/P95、每条用例的平均模型调用数与 token;小样本 P95 只作参考,注明样本量和模型 | `observations`;线上看 `conversation_log` 的 `duration_ms`/token 按 `intent->>'mode'` 分组 |
| 写操作是否可靠? | 只读请求、无排期权限、取消、过期卡和参数变化下,测试后端实际写入为零;正常确认核对目标、执行次数与最终场次;响应丢失时期望「状态待核实」且不自动重发 | 离线:`ts-write-*`、`ts-intent-*` 的 forbidden 断言 + `test_web_confirm.py`;跨服务:§9 |

对照报告(真实模型产物)放 `evals/reports/`,按 `intent_<日期>_<模型>.md` 命名,不进 PR 检查;
确定性回归(`intent_probes`、Web 测试)随 `uv run pytest` 在每个 PR 跑。

### 7.4 样本扩充

先用合成对话;拿到有授权、已匿名化的社团反馈后,补进 holdout 的**新 group**(不改旧 group),
并 bump 数据集后重录基线。真实用语进仓库前过 `security/pii.mask_pii`,姓名、学号、手机号一律换成
占位符。

## 8. 启用条件

第一周量过 off 基线后**锁定判断标准,之后不改**。建议的讨论稿(不代表现有成绩或统计显著性):

1. 固定的写操作与权限回归全部通过:`ts-write-*`、`ts-score-*`、`ts-intent-pick-without-options`、
   `ts-intent-negation-lookup` 在 hint 档零失败;`intent_probes` 与 pytest 全绿。
2. 在独立验收集(真实模型对照的 holdout 部分,扩到 40 条后)上**净改善至少 3/40 个任务**
   (原错新对 − 原对新错 ≥ 3)。
3. 端到端 P95 增幅不超过 10%。

| 结果 | 决定 |
|---|---|
| 三条都满足 | 生产开 `hint`,继续看一周 `conversation_log` 的错例 |
| 标签更准但任务没改善 / P95 超标 | 保留 `shadow`,在本文 §12 记原因 |
| 写操作或权限回归有任何失败 | 保持 `off`,先修再比 |

## 9. Java 测试环境联调手册

**准备**:Agent 指向 Java 测试环境(`BACKEND_BASE_URL`);固定一个测试管理员(持
`interview:schedule`)、一个无排期权限的管理员、一个候选人;固定一个开放招新周期、一份简历
(记下 resumeId)和三个同日场次(其中一个余量为 0)。每次操作记下 SSE 里的 `trace_id`。

| 情况 | 怎么触发 | Agent 侧期望 | 要核对的记录 |
|---|---|---|---|
| 成功 | 「周六还有空位吗」→「把 N 号简历排到第二个」→ 点确认 | `assign_interview` 返回 `done`;回复「已完成」 | `agent_audit_log` 两行(意图 + 结果 done);Java 后台该简历的场次 = 第二个;场次余量 −1;`conversation_log.intent.chosen_option` = 该场次 ID |
| 用户取消 | 同上,出卡后点取消;或不点卡直接发「先别改,看看周日」 | `cancelled`;后者本轮只查场次 | 审计**无**结果行;Java 后台安排不变;第二种情况 `intent.flags` 含 `negation`、`evidence` 含 `pending_cancelled` |
| 后端拒绝 | 排到余量为 0 的场次并确认;或用无权限管理员(看不到写工具,应只解释) | `error`,文案为后端的可行动提示;不重试 | 审计结果行 `error:`;Java 后台无变化 |
| 结果未知 | 确认后、后端返回前切断 Agent 到后端的连接(测试机上停后端进程或用代理丢弃响应) | `unknown`,提醒到管理页核实;**不自动重发** | 审计结果行 `unknown:`;到 Java 后台核实实际状态并记录;同一 trace 下只有一次写请求 |

另加两条「写入为零」检查:只读问法(「周六还有空位吗」)与过期卡(用旧指纹调 `/chat/confirm`,
期望 409)跑完后,审计表无新行、Java 后台无变化。每种情况留一份记录:时间、trace_id、对话截图、
审计行、Java 后台截图。

## 10. 四周排期

| 周次 | 本周工作 | 完成标志 | 现状 |
|---|---|---|---|
| 第 1 周 | 固定测试账户、招新周期和场次,走通查询与排期。整理标注、冻结验收集,跑当前 ReAct 基线 | 能重现一个有代表性的错例或确认基线已足够好;每个样例都有预期业务结果 | 标注集与执行器已就绪;**真实模型基线、测试环境未做** |
| 第 2 周 | 实现逐轮意图提示及开关,接入官网聊天,补多轮、否定和确认续跑测试 | 同一会话能演示「询问空位 → 指定场次 → 确认」和「指定场次 → 改口只查」两条路径 | **机制与测试已完成**;两条路径的真实模型演示未录 |
| 第 3 周 | 对照评测,对最常见的一类错误做一次修正;用 Agent + Java 测试环境核对取消、后端拒绝和结果未知 | 对照表与失败列表;能沿一次请求说明模型判断、工具调用和后端最终状态 | 对照脚本已就绪;**未执行** |
| 第 4 周 | 确定启用范围,接入固定回归,整理三分钟演示及两种岗位的讲法 | 一份带样本量和运行条件的报告、一段演示、一组只写本人贡献和实测数字的简历要点 | 固定回归已接入 pytest;其余未做 |

## 11. 演示脚本

管理员先问「周六还有空位吗」,页面给出可用场次。接着说「把 11 号简历排到第二个」,页面展示待确认
操作。先演示取消并核对记录没变,再重新选择并确认,打开 Java 后台核对最终安排。

- **Agent 面试**讲:系统怎样从上一轮的结构化工具结果理解「第二个」(§4 第 6 条),为什么只认上一轮;
  提示为什么只是参考而不是路由(§6);怎样用 off / hint 对照证明提示改善了工具选择(§7.2)。
- **Java 面试**讲:同一请求里本人身份怎样一路带到后端(本人 JWT,ADR-0006)、后端的权限与余量校验、
  后端结果未知时为什么不重试(`tools/write.py` 的执行顺序)。
- 只展开自己实际负责的代码。

## 12. 风险与已知局限

- **规则是中文词表**:换说法、错别字、方言会漏;漏判时落 `other`,不出提示,等于回到基线——失败
  方向是「没帮上」而不是「帮倒忙」。帮倒忙的风险在误判成写请求,所以模板对写请求一律要求
  「能唯一确定才调用,否则先问」,且写操作仍要确认。
- **验收集同源**:见 §7.1。在补进真实用语之前,holdout 的 100% 只说明规则覆盖了作者能想到的说法。
- **上一轮选项只认两类工具**:简历检索结果里的「第二个」不解析(落 `missing_target`)。
- **压缩**:会话被压缩后,上一轮若在摘要里而不在原始消息里,选项解析不到,按对象不明处理。
- **提示可能与模型自身判断冲突**:模板第一句即「与用户原话或工具结果冲突时以后者为准」;对照评测里
  「原对新错」那一栏就是在量这个风险。

## 13. 一个月后的方向

若工具选对但答错了规则,以本期社团简称和相似规则做检索评测,比较现有混合检索与单路检索
(`kb/store.py`)。若招新负责人更关心初筛质量,用独立的人工标注集比较现有单评委与复核模式,
记录分歧、耗时和成本(`evals/README.md` 的初筛评分基线)。两者都能复用本月积累的匿名错例与
回归方法。

## 14. 代码位置

| 内容 | 位置 |
|---|---|
| 规则判断、上轮选项解析、提示渲染 | `src/official_agent/graphs/assistant/intent.py` |
| 提示模板 | `src/official_agent/prompts/assistant_intent_hint.md` |
| 运行时上下文与 system prompt 拼接 | `src/official_agent/graphs/assistant/__init__.py`(`AssistantContext.intent_hint`、`_with_memories`) |
| Web 接入 | `src/official_agent/web/routes.py`(`_turn_intent`、`_stream_turn`) |
| 开关 | `src/official_agent/config.py`(`assistant_intent_mode`)、`.env.example` |
| 记录 | `src/official_agent/state/conversation.py`(`intent` 列)、`web/telemetry.py` |
| 规则门禁 | `evals/datasets/intent_probes.yaml`、`src/official_agent/evals/intent_probes.py` |
| 模型对照 | `evals/cases/tool_selection.yaml`、`src/official_agent/evals/tool_selection.py`、`evals/run_evals.py --intent-mode` |
| 对照报告 | `evals/compare_reports.py`、`src/official_agent/evals/compare.py` |
| 测试 | `tests/test_turn_intent.py`、`tests/test_web_intent.py`、`tests/test_evals_tool_selection.py`、`tests/test_evals_compare.py`、`tests/test_state_conversation.py` |
