# 多 agent 方向落地 · 交接文档

> 写于 2026-11-12,由上一任协调会话交给接手的会话。本文只记**当时的状态**与**没做完的事**;
> 各功能的设计、取舍与细节在 `docs/proposals/` 下的四份设计稿和各 PR 的描述里,这里不重复。
> 状态会过期:动手前先按 §2 的命令核对远端的实际情况。

## 1. 这件事是什么

用户要把 `docs/proposals/` 下四份多 agent 设计稿全部落地并上线,涉及三个仓库:

| 仓库 | 作用 | 这次的改动 |
|---|---|---|
| `Boyuan-IT-Club/Official_Web_Agent`(本仓) | AI Agent 服务 | 四条线的全部功能 |
| `Boyuan-IT-Club/Official_Web_Backend` | 后端 | 只加了 Agent 管理接口的代理路由 |
| `Boyuan-IT-Club/Official_Web_Frontend` | 管理端前端 | 对应的管理页面 |

四条线与分期(每条线的期号各自独立,见各设计稿 §10):

| 线 | 设计稿 | 分期 |
|---|---|---|
| 知识库运营闭环 | `kb-ops-loop.md` | P0–P3 |
| 初筛合议 | `screening-panel.md` | P0–P4 |
| 调查出题群 | `investigation-swarm.md` | P1–P4 |
| 周期复盘 | `cycle-retrospective.md` | P1–P4 |

## 2. 现在的状态(2026-11-12)

### 已合并并部署到生产(三个仓的部署工作流都已成功)

| 仓库 | 合并提交 | 内容 |
|---|---|---|
| Agent | `a9ed36f`(PR #224) | 四条线的 **P0–P3** 加评分段 token 用量落库。原来的 #218–#221、#223 已随之自动算作已合并 |
| 后端 | `7b0e62d`(PR #284) | 28 条代理路由,其中 2 条(`/evaluation/clauses*`)是给合议第四期预留的,目前只会透传 404 |
| 前端 | `a5ac805`(PR #281–#284) | 知识库周期选择器、周期指标快照页、知识缺口主题清单、初筛低置信队列 |

### 已在 GitHub 上、但还没合并

| 仓库 | 分支 / PR | 提交 | 是什么 | 为什么没合 |
|---|---|---|---|---|
| Agent | `feat/screening-panel`(PR #222) | `96d2487` | 合议 P4:评分条款候选 | 与 main 冲突(并存式) |
| Agent | `feat/investigation-swarm-p4`(PR #225) | `e0c79b5` | 出题群 P4:审题拦截模式、题型范例、给复盘的选题汇总 | 与 main 在 `.gitignore`、`docs/proposals/README.md`、`web/evaluation_admin.py` 三处冲突(并存式) |
| Agent | `wip/retro-p4-convergence` | `7311f16` | 复盘 P4 汇合 + 三项暂缺指标,**进行中的快照** | 未完成、未验证,不要合并 |
| 前端 | `feat/agent-kb-drafts-review`(PR #285) | `a085a0d` | 知识库草稿审核页 + FAQ 附加问法 | 与 main 在 `src/pages/AgentAdmin/index.tsx` 的 Tab 列表处冲突;另有一处待改(见 §4.2) |
| 前端 | `wip/retrospective-report` | `ceb4dff` | 复盘报告页,**进行中的快照** | 未完成、未验证,不要合并 |

两个 `wip/` 分支是协调会话在交接时从工作树直接截取的。原先做这两块的会话被要求收尾后再交一次正式提交;如果它们交了,会追加在本文末尾的「交接后的更新」里。**动手前请先核对远端:**

```bash
git fetch origin && git branch -r --sort=-committerdate | head -20
gh pr list -R Boyuan-IT-Club/Official_Web_Agent --state open
gh pr list -R Boyuan-IT-Club/Official_Web_Frontend --state open
gh pr list -R Boyuan-IT-Club/Official_Web_Backend --state open
```

## 3. 上线之后线上是什么样

**合并即生效的行为变化:**

- 初筛评分:缺省模式是 `critic`(评委 + 批判者 + 答辩)。还没出过卡的新周期直接用它,已出过卡的周期仍按单评委评完。**用户明确决定保持 `critic`**,尽管它没有在真实模型上跑过 eval。回退办法:部署配置里设 `EVALUATION_SCORING_MODE=single`
- 出题:证据线并发;GitHub 请求并发闸(4);模型并发闸 `EVALUATION_MODEL_CONCURRENCY`(默认 8);探索的整包封顶
- 知识库检索:每次检索落 `kb_query_log`;按招新周期过滤;FAQ 支持附加问法后分块方式有变
- 对话日志:新增 `trace_id`、`guard_verdict`,后者的格式是「守卫名=判定」
- 评审队列:低置信的卡排前面
- 新表新列由启动或首次使用时幂等建出,没有手工迁移

**默认关闭的功能**(各有测试守着默认值):知识库草稿起草 `kb_ops_drafting_enabled`、在线引用核验 `kb_citation_check_enabled`、忠实度抽样 `kb_ops_faithfulness_enabled`、审题标注 `QBANK_REVIEW_MODE`、题目索引 `QBANK_INDEX_ENABLED`、规划者 `QBANK_PLAN_MODE`、拆评委 `EVALUATION_SCORING_MODE=panel`、评分时看判例 `EVALUATION_CALIBRATION_ENABLED`、收割往届一致卡 `EVALUATION_CALIBRATION_HARVEST_ENABLED`、复盘报告生成 `RETROSPECTIVE_REPORT_ENABLED`、组织记忆召回 `ORG_MEMORY_ENABLED`。

**不要在没有 eval 证据的情况下打开这些开关。** 每个开关的启用条件写在对应设计稿的 §10 或「落地」小节里。

## 4. 没做完的事(按优先级)

### 4.1 上线后的一次性动作(需要有生产权限的人)

- **对当前招新周期触发一次指标快照**,留下这一届的基线:管理端「周期复盘」页点采集,或 `POST /api/admin/agent/retrospective/snapshot`,请求体 `{"cycle_id": <当前周期>}`,需要 `evaluation:run`。确认返回的 `domains` 里没有 `unavailable`。快照按周期日期切时间窗,晚采也能采到,但期间被删除的会话对应的日志会丢,所以越早越好。上一任协调会话访问不了生产,这件事还没做

### 4.2 前端 PR #285(草稿审核 + 附加问法)

1. 在它的分支上 `git merge origin/main`(用 merge,不要 rebase),解 `src/pages/AgentAdmin/index.tsx` 的冲突:两个 Tab 都保留,「知识缺口」在前、「草稿审核」在后
2. `src/api/manage/kbDraftsApis.ts` 里批准请求的 `timeout` 现在是 `15000`,要改回 `60000`:后端代理对这个接口的读超时已经是 60 秒(批准要重建向量)
3. 可选:草稿里的周期选择换成已在 main 的 #281 的选择器(带「是否开放中」的提示)
4. 跑完检查(见 §6)再推,CI 通过后合并

### 4.3 Agent 第四期

三块互相依赖,建议在**一个集成分支**上做完再合:

1. 从最新的 `origin/main` 切分支,依次 merge `96d2487`(合议 P4)和 `e0c79b5`(出题群 P4),解冲突。冲突都是「两边各自追加」:`.gitignore` 的数据集白名单、`docs/proposals/README.md` 的状态行、`web/evaluation_admin.py` 末尾各自加的接口、可能还有 `config.py` 与 `evals/engine.py` 的注册表
2. 把复盘 P4 做完。起点是 `wip/retro-p4-convergence`(`7311f16`,父提交 `5691454` 已在 main 里),里面有两件进行中的事:
   - **三项暂缺指标接上**:评估域评分段 token(来自评分段用量行,`tools` 列带 `scoring`)、知识库高频主题(来自 `kb_ops` 的主题清单)、低置信卡片改判率(来自评分卡的置信度)
   - **汇合进复盘报告**(`retrospective/handoff.py`):知识库缺口清单、评分条款候选、题型偏好三个栏目
   - 两个上游的只读出口(纯代码,不调模型):
     - `official_agent.state.evaluation.clause_store.list_candidates(cycle_id: int) -> list[dict]`(在 `96d2487` 里;每项含 `checklist`、`item`、`direction`、`clause`、`supporting_cards` 等;没有生成过返回空列表)
     - `official_agent.state.qbank.pick_type_summary(cycle_id: int) -> dict`(在 `e0c79b5` 里;形状见 `investigation-swarm.md` §5.2)
   - 约束:条款候选与题型偏好**只用代码放进报告**,不让分析员看简历原文,不新增模型调用;进报告的每个计数都要过小格子抑制;上游没数据时那一栏写「本周期没有」,不能让报告失败
   - 这个快照**没有跑过全量测试**,先跑一遍看哪里是红的
3. 全量测试通过后开 PR 合并。合并后 #222、#225 会自动算作已合并
4. 配套:后端要再补出题群 P4 的三条代理路由(`/admin/evaluation/exemplars`、`…/exemplars/propose`、`…/exemplars/{exemplar_id}/review`);前端要做「评分条款候选」页(只读 + 触发生成,**没有采纳按钮**)和「题型范例人审」页

第四期的所有功能都默认关闭,而且**现在验收不了**:它们需要至少一个完整周期的人工决策数据。目标是把机制做对、测试守住默认关闭。

### 4.4 前端其余页面

对应的 Agent 接口和后端代理都已上线(功能本身多数默认关闭),契约在本仓 main 的设计稿里:

| 页面 | 契约位置 | 备注 |
|---|---|---|
| 复盘报告(生成、查看、发布) | `cycle-retrospective.md` §5「报告接口契约」 | 起点是 `wip/retrospective-report`;关着时 `POST …/run` 返回 503,要显示成「未启用」 |
| 组织记忆(勾选、退役、续期) | `cycle-retrospective.md` §5「组织记忆接口契约」 | 勾选的文字会进内部对话的提示词,页面上要说明 |
| 校准案例管理 | `screening-panel.md` §5.1 | 查询参数是 snake_case(`include_retired`);收割关着时返回 409 |
| 忠实度抽样 | `kb-ops-loop.md` §5「忠实度抽样接口」 | 只读;关着时 `POST …/runs` 返回 409 |
| 出题审题标注与查重 | `investigation-swarm.md` §5.1 | 不是新页面:在题库抽屉 / 打分工作台里展示 `review`、`similarity` 两个可选键 |
| 评分条款候选、题型范例人审 | 见 §4.3 | 等第四期 |

前端页面的共同约定:走后端代理 `/api/admin/agent/<Agent 路径去掉 /admin/>`;新路由的查询参数用 snake_case;接口不存在(404)或功能未启用(409 / 503)时正常降级,不能白屏;写操作二次确认;用户原话、简历片段只当纯文本显示;被小样本抑制的数不能显示成 0。

### 4.5 所有依赖模型的 eval 都没跑过

开发机没有模型 key,CI 也不跑 eval。这是这次上线最大的未验证项。需要有 key 的人按 `evals/README.md` 跑,尤其是:

- `scoring_probes` 的单评委基线(`EVALUATION_SCORING_MODE=single … --write-baseline`),然后对着基线跑 `critic` 模式和 `critic_probes`——这两组数是「缺省 `critic` 要不要保留」的依据
- 各线新增的模型 suite:`kb_ops_triage`、`kb_ops_verify`、`kb_ops_faithfulness`、`question_review_probes`、`plan_probes` 的模型段、`retro_probes`、`retro_injection`、`org_memory_probes`、`clause_probes`、`exemplar_probes` 的模型段

所有新增 prompt 都是首版,跑完大概率要改。

### 4.6 等用户决定的小事

- 后端代理:Agent 返回 422(请求体校验不过,`detail` 是列表)时,信封里的 `message` 是空串。这是既有问题,五个代理控制器都这样;修是一行,但会改变既有接口的返回。协调会话建议修,用户还没答复
- 前端 #285:与现有条目冲突的草稿目前只提醒、不禁止批准(契约写的是「醒目提示」)

### 4.7 已知问题(都不阻塞,记在这里免得重新发现)

- 知识库的缺口甄别等调用也排在模型并发闸里,超时包含排队时间;初筛批量评分把闸占满时,主题清单里会出现 `failed` / `out_of_budget` 的主题。是降级不是出错
- `kb_ops/draft.py` 的受限检索循环直接调模型,不过并发闸(该功能默认关闭)
- `EVALUATION_MODEL_CONCURRENCY` 名字叫「评估域」,实际同时管初筛、出题、知识库运营、复盘四处
- CI 只设了 `POSTGRES_URL`,没有设 `KB_TEST_DATABASE_URL`,**知识库的真库集成档在 CI 里是跳过的**;全量结果要在本地跑
- 后端代理的评审队列接口不转发分页参数,前端最多拿到 200 张卡
- 简历列表只在持 `evaluation:run` 时才取评审队列,只有 `resume:audit` 的评审在列表上看不到低置信标签
- Langfuse 开启时,审计表的 `trace_id` 是 span id,与对话日志、检索日志里的入口级 id 不同
- 知识库缺口分析与指标快照都只有手动触发,没有定时任务
- `CLAUDE.md` 里说 eval 是 CI 门禁,与实际不符

### 4.8 没有立项、只是讨论过的

- 最初建议的六个方向里,「面试官 Copilot 的多 agent」与「把分级记忆补完整」没有展开成设计稿
- 意图识别:入口的意图分类是 ADR-0003 有意砍掉的;适合做的四处(对话日志离线标注、需要人工跟进的诉求、简单问题降档、软路由提示)只是建议

## 5. 用户说过的话(按时间)

接手后先读这一节。**这些授权是给上一任协调会话的,不自动延续给你**;尤其是合并到 main(等于部署生产),动手前先向用户确认一次。

| 原话 | 含义 |
|---|---|
| 「帮我按照你给的这些技术方案开工上线吧」 | 起点 |
| 「前端帮我也一起做了吧 剩下几期也做了」 | 范围扩到前端和全部分期 |
| 「P2、P3 各期做完后直接提pr吧」 | 做完直接提交、开 PR,不逐期询问 |
| 「在后端仓开工」 | 同意在后端仓补代理路由 |
| 「帮我把第四期也做了吧 然后和前后端一起上线」 | 第四期解除暂缓 |
| 「先把三期及以前的上线了吧」 | 先上 P0–P3 |
| 「保持批判者」 | 缺省评分模式保持 `critic`,不改回 `single` |
| 「合并并部署」「以最快的速度合并并上线已经完成的部分」 | P0–P3 已据此合并部署 |
| 「让其它支线也以最快的速度完成任务并上线」 | 其余部分做完一批上一批 |
| 「把现在已经做的都提交到GitHub上去吧 然后给我一份交接文档 我让另一个ai的新会话阅读并继续完成未完成的工作」 | 本文的来由 |

## 6. 怎么干活

### 合并就是部署

三个仓 push 到 `main` 都会**自动部署生产**(Agent 的 `docker-push.yml` 对 `**.md`、`docs/**`、`evals/**` 的改动除外)。所以:

- 只合「全量检查通过、新功能默认关闭」的提交
- Agent 仓用 **merge commit** 合并,不要 squash 或 rebase:各条线的分支是叠着的,squash 会让后面的分支全部冲突
- 已推送的提交不要改写(不要 amend、rebase、force push)

### GitHub 账号

这台机器的 `gh` 登录了两个账号。社团仓库的推送与 PR 一直用 `JJ20LOVE`;默认激活的另一个账号对社团仓库没有推送权限。上一任的做法是逐条命令指定,不切换全局账号,而且**事先得到了用户的明确同意**:

```bash
GH_TOKEN="$(gh auth token --user JJ20LOVE)" git push origin <提交>:refs/heads/<分支>
GH_TOKEN="$(gh auth token --user JJ20LOVE)" gh pr create -R Boyuan-IT-Club/<仓库> ...
```

接手后先问用户用哪个账号。在 zsh 里写 `"${C}:refs/heads/x"`,不要写 `"$C:refs/heads/x"`——后者的 `:r` 会被当成修饰符。

### 各仓的检查

- **Agent**:`uv run ruff check .`、`uv run pytest`。`ruff format` 不是门禁,不要整文件重排。测试需要 Postgres(pgvector):没有库时不是失败而是**卡住**(连接池 30 秒超时)。做法见 `deploy/docker-compose.local.yml` 的 `agent-pg` 服务;建一个一次性库,同时设 `POSTGRES_URL` 和 `KB_TEST_DATABASE_URL` 指向它,用完删掉。**不要把测试指向开发库 `official_agent` 或别人的库**——知识库集成档会做破坏性 DDL;本机 5432 端口上是另一个项目的库
- **前端**:类型检查、lint、单测、构建(命令见 `package.json`)。单测要加 `CI=true TZ=UTC` 跑一遍:CI 跑在 UTC,写死本地时区的用例在那里会失败(已经踩过一次)
- **后端**:`./mvnw -B clean test`,要 JDK 17

### 项目规矩(详见 `CLAUDE.md` 与 `docs/standards/`)

- 简历是学生 PII,也是不可信输入:进模型前脱敏、包数据区;批判节点负责检测提示注入
- 写操作必须人工确认;新增写操作要同时有确认、审计日志、eval 用例
- 权限边界放在工具装配层,不靠 prompt
- 观测 fail-open,写路径 fail-closed
- Agent 的输出只是辅助参考,不替人做录取决定
- 注释用中文,不写 issue 号、迭代代号和改动叙事
- 新 prompt 放 `prompts/`,新模型角色路由默认 strong
- 没验证的不要写成已验证

### 上一任的分工方式(供参考)

每条线一个会话,各自在独立的 git worktree(`.claude/worktrees/` 下)里做,做完验证后本地提交,把提交号报给协调会话;协调会话核对提交、扫一遍凭证、推成 PR、等 CI、合并。那些会话在交接时已被要求收尾停下。

## 7. PR 索引

**Agent**(`Boyuan-IT-Club/Official_Web_Agent`)
- 已合并:#218 知识库 P0、#219 知识库 P1–P3、#220 出题群 P1–P3、#221 复盘 P1–P3、#223 评分 token 用量、#224 集成发布
- 开着:#222 合议(只剩 P4 一个提交)、#225 出题群 P4

**后端**(`Boyuan-IT-Club/Official_Web_Backend`)
- 已合并:#284 代理路由

**前端**(`Boyuan-IT-Club/Official_Web_Frontend`)
- 已合并:#281 周期选择器、#282 指标快照页、#283 知识缺口清单、#284 低置信队列
- 开着:#285 草稿审核 + 附加问法

各 PR 的描述里有该部分的完整说明,包括「没有验证的」和「需要 reviewer 知道的」。

## 交接后的更新

(原会话收尾时交出的正式提交会追加在这里。)
