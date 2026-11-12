# 调查出题群:规划者 + 并行探索者 + 对抗审题

> 提案 v0.1 · 2026-11-11 · 状态:**P1 已实现**(证据线并发、汇合处去重、GitHub 请求并发闸;
> 真实模型下的墙钟与质量对比尚未跑)。**P2 的机制已实现但默认关闭,验收未完成**(审题者只
> 标注 + 题目索引;`question_review_probes` 没有在真实模型上跑过,相似度门槛没有校准,见 §10
> 进度)。**P3 的机制已实现但规划者默认关闭,验收未完成**(分档预算、整包封顶、规划者;
> `plan_probes` 的确定性段已通过,模型段没有跑过)。**P4 的机制已实现,两项都默认关闭,启用
> 条件都没有满足**(审题拦截要误杀率达标,题型范例要至少一个周期的选题数据;只用合成数据
> 测过机制)。范围 = B 模块出题线
> (`evaluation/bundle.py`、`evaluation/investigate_graph.py`、`evaluation/explore.py`)与题库
> (`state/qbank.py`);**不改评分子图,不改面试官选题的交互**。共同约束见 [README](README.md)。

## 1. 背景与问题

P1 之前的现状(「串行」那一条 P1 已经改掉,留在这里说明问题从哪来;其余仍可在代码里查到):

- `run_bundle` 依次跑四类证据线:仓线(每个可读的仓一条,调查子图 `route → explore → generate`)、
  评测线、奖项线,以上全无时的兜底线。最后 `suggest_plan` 给一个 15 分钟的建议组合。
- 仓线在一个 `for` 循环里**串行**。每条仓线的探索是受限循环,双闸为 80 轮 / 300 秒
  (`explore.MAX_TURNS`、`MAX_WALL_SECONDS`);仓线上限 5 条(`MAX_REPO_LINES`,注释写得很直白:
  不封顶就等于把成本开关交给候选人)。最坏情况下光探索就是 5 × 300 秒。
- 每个仓拿到的预算**一样**:一个 fork 来的教程仓和候选人写了半年的主项目,探索深度相同。
- 探索者把材料写进档案(`Dossier`,十个类别槽位,上限 4 万字符),出题者只看档案。这个上下文
  隔离做得对,本提案沿用。
- 出题有确定性校验(`validate_qbank_v2_group`:证据路径、链层数、对抗性措辞等),但**没有模型
  审题**。`qbank_judge` 是离线 eval,不在生产路径上。
- 各线各出各的题,**线与线之间不查重**;不同候选人之间也不知道哪些题其实是通用题。
- 面试官选了哪些题记在 `qbank_pick_log` 里,目前没有任何地方用它。

| 问题 | 表现 |
|---|---|
| 慢 | 多仓简历的出题耗时随仓数线性增长 |
| 预算不分轻重 | 探索轮数平均摊在不值得深挖的仓上 |
| 没有第二双眼睛 | 「搜一下就能答」「背得出来」的题只能等面试官自己筛掉 |
| 题目重复 | 同一个点在两条线上各问一遍;不同候选人拿到近乎相同的题 |
| 选题信号没回流 | 面试官的偏好年年重新摸索 |

## 2. 目标与非目标

**目标**

1. 现有证据线并行跑,出题耗时不再随仓数线性增长。
2. 探索预算按证据线的价值分档,总预算有硬上限。
3. 每道题在出库前过一遍独立审题,先标注,证据充分后再拦截。
4. 题目向量索引:包内查重,跨候选人识别通用题。
5. 面试官选用的题沉淀成去标识的「题型」,作为出题范例。

**非目标**

- 不放宽 `MAX_REPO_LINES`,不让模型或候选人决定预算数值。
- 不改调查子图内部的路由与归属判断。
- 不做「一个大出题者看全部档案」(D4)。
- 不让审题者改写题目(它只有放行、打回、丢弃三种意见)。
- 不把面试官没选的题当负例(D9)。

## 3. 总体流程

```mermaid
flowchart TD
    C[确定性枚举候选证据线] --> PL[规划者:分档与侧重]
    PL --> B[代码:档位映射成预算并封顶]
    B -->|Send 每线一路| E1[仓线:探索、出题、审题]
    B -->|Send| E2[评测线:出题、审题]
    B -->|Send| E3[奖项线:出题、审题]
    E1 --> M[汇合:简历深挖只留一条,无证据则兜底]
    E2 --> M
    E3 --> M
    M --> IDX[(题目索引:跨线查重与通用题检测)]
    IDX --> SP[suggest_plan 建议组合]
    SP --> QB[(interview_qbank)]
    QB --> PK[面试官选题]
    PK --> LOG[(qbank_pick_log)]
    LOG -.周期结束后,人审.-> EX[(选用题型)]
    EX -.下一周期.-> E1
    EX -.-> PL
```

## 4. 关键决策

### D1 · 第一步只做并行化,不加任何新角色

把 `run_bundle` 里的串行循环改成并发,就能拿到最大的一块收益,而且不增加一次模型调用。要处理的
只有一个线间依赖:**简历深挖全局只出一条线**。现在靠 `cv_dive_done` 按顺序传递——前面的线走了
简历深挖,后面读不到仓的线就直接跳过。

并行后的做法:各线不再互相等,汇合时由代码按线的原始顺序保留第一条简历深挖组,丢掉其余。代价是
极少数情况下多花一次出题调用(可读的仓在子图内探测时恰好瞬时失败才会触发),换来的是线间没有
共享的可变状态。

并发度受两个闸约束:评估域并发闸(见 README),以及 `GitHubClient` 上的请求并发上限——并行的
探索者共用同一个令牌的速率配额。

**P1 落地时的取舍**(与上文不一致处以这里为准):

- **扇出用 `asyncio.gather`,不是 `Send`**。`run_bundle` 本身不是图,各线是普通协程;等规划者
  (P3)进来、编排需要成图时再换。仓线、评测线、奖项线三路同时跑,分线前的仓可读性探测也并发。
- **仓线并发度在包内先封顶 3**(`bundle.MAX_PARALLEL_REPO_LINES`),把 D2 的「并行探索者至多
  3 个」提前落了地。封顶的 5 条仓线分两批跑完:整包墙钟约为最慢一条线的两倍,而不是五条之和。
- **评估域模型并发闸在 P2 补上了**(P1 上线时没有它:一个 bundle 的模型调用峰值是 4——3 条
  仓线加评测线——4 个 job 同时在题库段时评估域峰值到 16)。现在评分、探索、出题、审题的每一次
  模型调用都先过 `llm_common.model_gate()`,进程内一把,上限是配置项
  `EVALUATION_MODEL_CONCURRENCY`(默认 8,即把上面的 16 封到 8)。8 没有按模型供应商的限流实测
  过:模型侧一旦限流,表现不是报错而是探索降级、题目变薄,见到这种迹象就调小。评分子图没有
  改代码,它本来就经 `invoke_with_retry` 调模型,闸设在那里面。**排队等闸的时间不计入探索的
  300 秒墙钟**:预算管的是探索自己花多久,不该因为别的 job 忙而让这条线的档案变薄;代价是整包
  墙钟在忙时没有硬上限(runner 对在跑的 job 没有超时回收,所以不会因此重复执行)。
- **GitHub 请求闸**:`github_client.MAX_CONCURRENT_REQUESTS = 4`,进程内所有 client 共用(各线
  各建各的 client),包住全部出站请求。取 4 是让峰值不高于「每个 job 同一时刻只发一个请求」的
  水平。闸只限并发,不做 `Retry-After` 退避:被限流仍按「仓不可读」降级,与现状相同。
- **「丢掉其余」= 整组不进信封**,留一条 `bundle_cv_dive_dedup` 日志;被丢掉的线花的 token 仍
  计入 `explore_usage_total`(用量在去重之前聚合)。这与分线阶段「读不到的仓只留一条线」的
  处理一致:多出来的仓在信封里不留痕。
- **多花的调用不是独立小概率事件**。触发条件是「探测时可读、进子图时读不到」,而限流会让同一
  批线一起读不到:最坏是每条仓线各做一次简历深挖(各含一次技术栈抽取和一次出题),上限 5 条。
  第一批线紧跟着探测起跑,窗口只有一次请求那么长;排在第二批的线要等前一批跑完,窗口是分钟
  级的。两种情况都与 GitHub 限流正相关。
- 子图的 `cv_dive_done` 参数保留(非目标:不改子图路由),`run_bundle` 不再传它(§12 第 8 条)。

### D2 · 规划者只选档位和侧重,预算由代码映射并封顶

规划者是单次调用。输入是代码枚举出来的候选线(仓名、仓的元信息、归属级别)和简历里的项目
文本;输出是每条线的档位与侧重类别。

| 档位 | 探索双闸(初值) | 典型对象 |
|---|---|---|
| `high` | 80 轮 / 300 秒 | 归属可信的主项目 |
| `normal` | 40 轮 / 180 秒 | 一般的自有仓 |
| `low` | 20 轮 / 90 秒 | fork、教程仓、课程作业模板 |

约束都在代码里,模型越不过去:

- 规划者**不能发明证据线**:输出的线必须是候选集合的子集,多出来的丢弃。
- 侧重类别必须取自现有的十个档案类别(`schema.CATEGORY`)。
- 整包硬上限:并行探索者至多 3 个,总探索轮数与整包墙钟各有上限;超出时从低档往下砍。
- `high` 档就是今天所有仓线的默认值——规划者只能**省**预算,不能加。

侧重提示放进探索者的**用户消息**,不进 system 文本:探索的 system 文本保持稳定是现有的缓存纪律。

规划者失败或输出不合规时,所有线退回今天的默认(全部 `high`、无侧重)。规划是优化,不是依赖。

**P3 落地时的取舍**(与上文不一致处以这里为准):

- **规划者的开关是 `QBANK_PLAN_MODE`,默认 `off`**:关着时不调模型,每条线都是 `high`,与没有
  规划者时一样。它的 prompt 还没有在真实模型上跑过;打开的前提是 `plan_probes` 的模型段跑过
  并达标(§8)。
- **整包封顶与规划者开没开无关,始终生效**。两个上限是配置项
  `QBANK_EXPLORE_MAX_TOTAL_TURNS`(400)和 `QBANK_EXPLORE_MAX_TOTAL_WALL_SECONDS`(1500),
  缺省值正好是「5 条仓线全部 `high`」的总量——不比没有上限时更紧,要省就往小里调。墙钟上限
  算的是**各线墙钟预算之和**,不是整包实际花的时间(仓线并发,实际更短)。
- **「从低档往下砍」的规则**:超了就反复取当前档位最低的那条线(同档取排在最后的)降一级
  ——`low` 降到砍掉,`normal` 降到 `low`,`high` 降到 `normal`——直到两个上限都满足。被砍掉的线
  不跑、不出组,计划里记 `dropped`,日志留 `bundle_plan_cut`。所以先牺牲的是规划者认为最不值得
  读的仓;没有规划(全是 `high`)时牺牲的是简历里排在最后的仓。任何输入、任何上限下结果都不
  超限:上限小到一条线也容不下时,所有仓线都被砍掉。
- 「并行探索者至多 3 个」就是 P1 的 `bundle.MAX_PARALLEL_REPO_LINES`,没有另设。
- **只给可读的仓定计划**:读不到的仓不进探索段,没有预算可言。规划者看到的元信息就是分线
  探测时拿到的那份仓元数据(描述、语言、是否 fork、star、体量、最近推送),不为它多发请求;
  归属级别要进了子图才算得出来,所以给规划者的是「仓主是不是候选人绑定的登录名」。
- **输出的收口全在代码里**:候选集合之外的线丢弃;规划者没提到的候选线按 `high`;同一条线
  出现两次取第一次;档位或类别不在固定集合里、或多出任何字段(比如想自己写 `max_turns`)都被
  解析器拒收,回灌纠错重试一次,仍不合规就退回缺省计划(`status=fallback`)。侧重去重后至多
  3 个。
- **预算只能省**:探索段把调用方给的预算收回自己的缺省上限以内(`ExploreBudget.clamped`),
  所以即使映射表写错,一条线也拿不到比今天更多的预算。
- **计划始终进信封**(`plan`,规划者关着也记):每条线给了多少预算、实际用了多少轮和多少秒。
  §12 第 1 条要的校准数据从这一版起就在攒,不必等规划者打开。
- `normal` / `low` 两档的数值(40 轮 / 180 秒、20 轮 / 90 秒)仍是初值。

### D3 · 探索者之间上下文隔离

每个探索者有自己的档案和自己的预算,互相看不到。回到主流程的只有出题结果和用量——仓里的文件
原文不离开那条线。这与现状一致,并行之后更要守住:一个仓里的注入文本不应该有机会影响另一条线。

### D4 · 出题仍然每线一次;跨线只做组合与去重

没有采用「探索者只交档案,由一个出题者统一出题」的方案,原因有三:

- 证据路径校验是按仓做的,合并出题后一道题的证据属于哪个仓会变得含糊;
- 五份档案加起来可以到 20 万字符,一次调用塞不下也读不好;
- 一条线失败只影响一组题的隔离性会丢掉。

跨线要做的事——去重、挑出建议组合——用代码和向量索引就够了(D7 与现有的 `suggest_plan`)。

### D5 · 独立审题者:先只标注,有证据再拦截

审题者是单次调用,在每条线内部、出题之后跑一次,看到的是该线的题组和它的证据材料(仓线是
档案)。先过确定性检查(现有的对抗性措辞表等),再由模型逐题给意见:

| 意见 | 含义 |
|---|---|
| `keep` | 放行 |
| `revise` | 打回,附理由 |
| `drop` | 建议丢弃,附理由 |

理由取自固定的集合:

| 理由 | 含义 |
|---|---|
| `searchable` | 搜一下就能答,和候选人的项目无关 |
| `ungrounded` | 题目声称的事实在档案里找不到 |
| `recitable` | 背八股就能答,问不出是不是自己做的 |
| `duplicate` | 与同组里另一道题问的是同一件事(跨线的重复由题目索引处理,见 D7) |
| `generic` | 换一份简历照样能问 |
| `tone` | 质问、设套、让人难堪的措辞 |

两种模式,由配置切换:

- **`annotate`(默认)**:只把意见挂在题上,题库原样落库。面试官看得到标注,选题数据可以反过来
  检验审题者准不准。
- **`enforce`**:`revise` 的题打回出题者改一次;`drop` 的题丢弃——前提是丢掉之后题组仍然满足
  现有的结构约束,否则保留并标注。

`enforce` 只在 eval 证明审题者的误杀率够低之后才开。审题者没有改写权:它改的题没有人再审。

**P2 落地时的取舍**(只实现了 `annotate`;与上文不一致处以这里为准):

- **意见挂在题组旁边,不进题目对象**。题目对象是出题模型的输出面:在那里留一个 `review`
  字段,出题模型(或仓里的注入文本)就能替自己的题预先填上「放行」。所以每条线的审题结果是
  信封上的一块 `review`(`schema.LineReview`:状态、逐题意见、审题 prompt 版本、用量),
  逐题意见用组内编号(`entry` / `chain:链:层` / `reserve:序号` / `question:序号`)对回题目;
  题库的可挑题视图读出时再把意见并到题上(§5.1)。审题 prompt 版本因此记在线上,不逐题重复。
- **「附理由」就是理由代号**,没有自由文本。审题者读到的题目与仓内容同源,让它写一句话等于给
  注入留一条落库的通道。结论与理由的搭配由代码校验:`keep` 不带理由,`revise` / `drop` 至少一条。
- **审哪些线**:题目由模型依据证据材料写成的三种——仓深挖、简历深挖、评测线。通用引导题
  (材料读不到时的兜底,通用是它的本分)、奖项线(模板题)、兜底线(按定义就是通用题)不审。
- **入口题不记 `generic`**:prompt 里说了,代码里再剔一遍(模型给了也不算)。
- **确定性预检只有两条**:措辞词表(与出题后的结构校验共用一份,`investigate.ADVERSARIAL_WORDS`)
  和同文重复(标后出现的那道)。仓线的题在出题校验时已经过了词表,预检主要对评测线有用。模型
  判了 `keep` 而预检查出问题的题按 `revise` 算。
- **审题失败**:结果标 `unavailable`,题目原样放行,只留预检查出来的那几条(D10)。
- **开关是 `QBANK_REVIEW_MODE`,默认 `off`**,不是 `annotate`。审题给每条有题的线多加一次
  strong 调用,输入是整份档案;而它的 prompt 还没有在真实模型上跑过一次(开发机没有模型 key)。
  打开的前提是 `question_review_probes` 跑过并达标(§8)。`enforce` 这个取值 P2 不认,按 `off` 处理。
- **缓存命中做不到**(§9 原来的设想):审题与出题是两份 prompt,开头不同,档案不是共同前缀。
  要命中得改出题 prompt 的排布,那会动到出题行为,不在 P2 里做。

**P4 落地的拦截模式(`enforce`)**——机制已实现,**启用条件没有满足,现在不能打开**:

- **要两项配置同时到位才生效**:`QBANK_REVIEW_MODE=enforce` 加 `QBANK_REVIEW_ENFORCE_READY=true`。
  只写了前者按 `annotate` 处理并留一条告警。拦截会改题、丢题,不该因为抄错一处配置、或有人
  提前把模式改好就开始。第二项置真的前提是 `question_review_probes` 的 `good_keep_rate`
  (好题没被误伤的比例)在真实模型上达标——这个 suite 还一次都没跑过。
- **只作用于仓线**(仓深挖、简历深挖)。评测线在拦截模式下仍然只标注:它的题是另一条出题
  路径出的,没有同一套结构校验可以兜底。
- **打回(`revise`)**:把出题时的同一份 prompt 接上一段打回要求(`prompts/evaluation/
  question_revise.md`,带着上次的题组和被打回的题,都包数据区),让**出题者**改一次。
  - 换题由代码做,只换被打回的那几道(`question_review.replace_questions`):出题者在重写时
    顺手改了别的题、挪了位置,都带不进来——没被打回的题是审题者看过的。
  - 改过的题必须有人再审:换完之后整组复审一遍;复审拿不到可用结果,这次重写就不采用。
  - 只改一次:复审里仍被打回的题不再重写,意见留着。重写两次都不合规也是保留原题。
- **丢弃(`drop`)**(`question_review.drop_flagged`):备选题可以丢;追问链只从**尾部**往前
  丢(后一层以前一层为前提,中间的层不丢);入口题不丢。每丢一道都要过一遍出题时的结构校验,
  过不了就留着、意见照旧挂着。被丢的题连同题文记在审题结果的 `dropped` 里备查——它不在题组
  里,进不了可挑题视图。
- 审题模型没给出可用结果时不拦截,题组原样放行。
- 拦截模式下每条线至多多一次出题调用和一次审题调用,两者的用量都记在审题结果里并计入聚合。

### D6 · 审题标准与离线 judge 分开

`qbank_judge` 按相关性、具体性、公平性、区分度四个维度给整套题打分,是离线度量。在线审题者如果
用同一套标准,就成了「对着考卷改答案」——judge 分数会涨,但不说明题目变好了。

所以两者的标准故意不同:审题者做逐题的缺陷检查(D5 的理由集合),judge 做整套题的质量评分。
最终的裁判是人:面试官的选题记录,以及 `cv_dive_report` 的人工评审。

### D7 · 题目向量索引

每道可选的题落一行索引,主键沿用题库现有的稳定题引用 id(`state/qbank.py` 的 `_ref_id`),
这样和选题记录天然对得上。

两个用途:

- **包内查重**:同一份简历的各线之间,相似度过门槛的题只保留证据更强的一道(仓线优先于简历线)。
- **通用题检测**:一道题如果和至少 N 份其他简历的题近乎相同,标 `generic`。入口题允许通用(开场
  本来就是通用的),链题和备选题不允许。

只在本周期内比较。嵌入模型与知识库同一个,遵守 README 的向量表纪律。

**P2 落地时的取舍**(与上文不一致处以这里为准):

- **只标注,不删题**。包内查重没有「只保留一道」:证据弱的那道记 `duplicate_of`,指向留下的
  那道;通用题记 `generic_resumes`(有几份其他简历出过近似的题)。删题要等 P4 的拦截模式——
  门槛没有校准之前删题,等于拿一个拍出来的数字替面试官做决定,而且从链里抽掉一层会破坏题组结构。
- **入口题不会被标**(既不记重复也不记通用):各线的开场题措辞相近是正常的。它仍可以是别的
  题的「强题」。
- **证据强弱的次序**:仓线 > 评测线 > 简历线 > 奖项线 > 兜底线;同类按线的先后。两道以上
  留下的题都过门槛时,指向最像的那道。同一组里的重复不归索引管(那是审题者的 `duplicate`)。
- **标注放在信封顶层的 `similarity`**,按「组下标 + 组内编号」定位,理由同 D5:不进题目对象。
- **这一步在 runner 的题库段,不在 bundle 里**(§3 的流程图把它画在 `suggest_plan` 之前)。
  标注要写进信封,所以在落库前;索引行的主键是稳定题引用 id,而它含题库版本,版本要落库后才有,
  所以写索引在落库后。P2 的标注不影响建议组合,先后不要紧。
- **索引表**(`state/qbank_index.py`,§5):没有 `picked` 列——主键就是题引用 id,与选题记录
  关联即可;多了 `role`;向量列不带维度,比较时用 `CASE` 把不同模型的行挡在外面(换模型后新旧
  向量维度不同,直接比会报错);一份简历在一个周期里只留最新一版题库的行。
- **开关是 `QBANK_INDEX_ENABLED`,默认关**。三个数——`QBANK_DUPLICATE_MIN_SIMILARITY`、
  `QBANK_GENERIC_MIN_SIMILARITY`(都是 0.9)、`QBANK_GENERIC_MIN_RESUMES`(3)——全是初值,
  没有拿任何真实题库量过(§12 第 2 条)。打开还需要 `EMBED_*` 已配置、题库所在的库装有 pgvector;
  缺任何一样都只是没有标注,题库照常落库。
- **没有回填工具**:开关打开之前落库的题库不在索引里。要「先拿现有题库量一遍分布」,得先写一个
  把 `interview_qbank` 里的旧信封重新算向量入索引的脚本——这件事 P2 没有做。
- 题文在算向量、入索引之前过 `mask_pii`。

### D8 · 范例只用去标识的「题型」

面试官选过的题带着候选人的项目细节,不能直接拿去给另一份简历当范例。沉淀的是**题型**:把具体
项目抹掉后的提问模式,例如「让候选人解释某个提交里为什么改了数据结构」。

题型由离线一次调用从选用记录里归纳,人审通过后才生效(共同约束第 8 条)。检索时按档案类别与
年级带过滤,取少量几条放在出题 prompt 的范例段,包数据区,并声明「范例只说明提问方式」。

### D9 · 选用是弱正例,没选不是负例

面试官选题有很强的位置偏差(排在前面的更容易被选)和时间约束(15 分钟只能问几道)。所以:

- 被多位面试官、在多份简历上反复选用的**题型**,才当作正例;
- 没被选的题不说明任何问题,不进任何负例集合;
- 题型的统计只用聚合计数,不记到具体面试官头上。

**P4 落地的题型范例**(D8、D9)——机制已实现,**启用条件没有满足,现在不能打开**:还没有
任何一个完整周期的选题数据,归纳用的 prompt 也没有在真实模型上跑过;机制只用合成数据测过。

- **一个开关管全部**:`QBANK_EXEMPLARS_ENABLED`,默认关。关着时不读选题记录、不调归纳模型、
  管理面的两个写接口返回 409、出题不带范例。
- **归纳**(`evaluation/exemplar.py`,由管理员在管理面触发,不自动跑):
  - 素材是一个周期的选题记录:同一位面试官重复勾同一道题只算一次;只取十个题类里的题
    (评测线、奖项线、兜底线的题不归纳);题文先脱敏,再包数据区交给模型。
  - 模型的每条输出都要过代码的闸(`gate_pattern`):空或过长、含个人信息、含指令样文本、
    带着网址 / 文件名 / 仓内路径的,一律拒收。闸拦不住「项目名写成了普通词」这种情况,
    那要靠人审。
  - 计数不取模型的说法:被选次数、涉及几份简历、几位面试官,都由代码按来源重新数;来源里
    清单之外的编号不算数。
  - D9 的门槛写成了数:至少 2 位面试官、至少 2 份简历选用过才成题型。
  - 产出是草稿,整批替换该周期原有的草稿;已经有人审过的题型不动。被拒收的只回报原因与
    计数,不带题型文字。
- **人审**:通过(`approved`)或停用(`retired`)。状态只能往前走——草稿→通过 / 停用,
  通过→停用,停用之后不能再通过。两个写操作都先写审计再执行,审计写不了就不执行。
- **取用**:出题段取已通过人审的题型,选用面最广的在前,至多 5 条,每条再过一遍闸;接在
  出题 prompt 的最后(`prompts/evaluation/exemplars.md`),包数据区,并声明范例只说明提问
  方式、不是关于这位候选人的事实。只给仓深挖与简历深挖。用到了哪些题型、范例段的版本,
  随题库记在 `qbank_v2.exemplars` 里。题型表读不到时不带范例照常出题。
- **与原稿不同的地方**:没有做向量检索(一个周期能通过人审的题型是个位数到几十条,按
  选用面排序就够了,表里没有 `embedding` 列);没有按题类和年级过滤(选题记录里没有年级,
  表里的 `grade_band` 列留着但恒为空)。
- 没被选的题不进任何集合;计数只到「几位」,库里不存是哪位面试官选的。

### D10 · 失败语义

| 失败点 | 语义 |
|---|---|
| 规划者失败 | 退回默认计划(D2) |
| 单条证据线失败 | 降级为空的错误组,不炸整包(与现状相同) |
| 审题者失败 | 题目原样放行,标注「审题不可用」 |
| 题目索引写入或检索失败 | 跳过查重,题库照常落库 |
| 范例检索失败 | 不带范例出题 |
| 整包失败 | 只影响出题状态,不影响已落库的评分卡(与现状相同) |

出题线全程只读,没有任何一步需要 fail-closed。

P1 的落地形态:仓线失败留一个 `mode=error` 的错误组;评测线、奖项线失败是整线缺席(沿用各线
原有的语义)。并发之后多了一层兜底——线内没料到的异常也止步于该线(`bundle._contained`),
否则整包失败之外,其余还在跑的线会成为没人收结果的孤儿任务,白花模型调用。

## 5. 数据面

**信封没有升到 v3**(原设想是升版本,并在每道题上加 `review`)。P2 只增加可选字段,读方不需要
按版本分支,`save_qbank` 仍然只认 `evaluation_qbank/v2`;没有这些字段的旧题库按「未审」处理。
P3 的 `plan` 若也只是可选字段,同样不必升版本。

P2 新增的三处都**不在题目对象里**(理由见 D5):

| 位置 | 内容 | 何时出现 |
|---|---|---|
| `groups[i].qbank_v2.review` | 仓线(仓深挖 / 简历深挖)的审题结果 | 审题打开且该线过审;否则为 `null` |
| `groups[i].review` | 评测线的审题结果,形状同上 | 审题打开;否则没有这个键 |
| `similarity`(信封顶层) | 相似度标注 | 题目索引打开且 embedding 可用;否则没有这个键 |
| `plan`(信封顶层,P3) | 探索计划:每条可读仓线的档位、侧重、预算与实际用量 | 始终有(规划者关着时 `status=off`) |
| `groups[i].qbank_v2.exemplars`(P4) | 这组题出题时带了哪些题型范例 | 题型范例打开且有已通过的题型;否则为 `null` |

```jsonc
// 一条线的审题结果(schema.LineReview)
{
  "status": "reviewed",              // reviewed | unavailable(审题模型没给出可用结果)
  "items": [                         // reviewed 时每道题一条;unavailable 时只有预检查出来的
    {"key": "entry",     "verdict": "keep",   "reasons": []},
    {"key": "chain:0:1", "verdict": "revise", "reasons": ["recitable"]},
    {"key": "reserve:2", "verdict": "drop",   "reasons": ["searchable", "generic"]}
  ],
  "prompt_version": "evaluation_question_review/v1",
  "usage": {"input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": null, "cache_miss_tokens": null},

  // 以下是 P4 拦截模式的字段;标注模式下取缺省值
  "mode": "annotate",                // annotate | enforce(enforce = 题组被这次审题改过)
  // items 里每条多一个 "revised": false——拦截模式下被打回并由出题者重写过的题为 true
  "dropped": [                       // 拦截模式丢掉的题;不在题组里,进不了 pickable
    {"key": "reserve:1", "question": "…", "reasons": ["searchable"]}
  ],
  "revise_prompt_version": "",       // 做过打回重写时是 evaluation_question_revise/v1
  "revise_usage": null               // 打回重写那一次出题调用的用量
}

// 仓线信封里的题型范例使用记录(P4)
{"prompt_version": "evaluation_exemplars/v1", "exemplar_ids": [7, 9]}

// 信封顶层的相似度标注:只列被标到的题
{
  "embed_model": "…",
  "items": [
    {"group_index": 1, "key": "reserve:0",
     "duplicate_of": {"group_index": 0, "key": "chain:0:0"},   // 本包里证据更强的那道;没有则 null
     "generic_resumes": 0}                                      // 几份其他简历出过近似的题;没被判通用则 0
  ]
}
```

```jsonc
// 信封顶层的探索计划(P3)
{
  "status": "planned",               // off 没调规划者 | planned 用了规划者的输出 | fallback 调了但退回缺省
  "prompt_version": "evaluation_plan/v1",   // off 时为空串
  "usage": {"input_tokens": 0, "output_tokens": 0},   // 规划调用的用量;没调为 null
  "lines": [                         // 只有可读的仓;顺序同仓线
    {"repo": "alice/club-site", "tier": "high", "focus": ["C6_难点与调试"],
     "max_turns": 80, "max_wall_seconds": 300,        // 给了多少
     "turns_used": 37, "seconds_used": 142.5},        // 用了多少;没进探索段或跑失败为 null
    {"repo": "alice/flask", "tier": "dropped", "focus": [],   // 被整包上限砍掉:不跑,不出组
     "max_turns": 0, "max_wall_seconds": 0, "turns_used": null, "seconds_used": null}
  ]
}
```

题目索引表(`state/qbank_index.py`)与题型表(`state/qbank_exemplar.py`)都不进启动流程,对应的
开关没打开就不会建表:索引表在第一次查索引或写索引时建;题型表只在第一次写草稿时建,读路径
(题型列表、范例取用、给复盘的汇总)不建表,表不存在就当作没有题型。

```sql
CREATE TABLE IF NOT EXISTS qbank_question_index (
    ref_id        text        PRIMARY KEY,           -- 稳定题引用 id,与选题记录对得上
    resume_id     bigint      NOT NULL,
    cycle_id      int         NOT NULL,
    qbank_version int         NOT NULL,
    group_kind    text        NOT NULL,
    role          text        NOT NULL,              -- entry / chain / reserve / question
    category      text        NOT NULL DEFAULT '',
    question      text        NOT NULL,              -- 已脱敏
    embedding     vector      NOT NULL,              -- 不带维度,见 D7
    embed_model   text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- 题型表(P4 已建,state/qbank_exemplar.py,惰性自举)
CREATE TABLE IF NOT EXISTS qbank_exemplar (
    exemplar_id       bigserial   PRIMARY KEY,
    cycle_id          int         NOT NULL,              -- 归纳自哪个周期的选题记录
    category          text        NOT NULL,
    grade_band        text        NOT NULL DEFAULT '',   -- 留着,恒为空(见 D8 的落地说明)
    pattern           text        NOT NULL,              -- 去标识的提问方式,过了闸才入库
    pick_count        int         NOT NULL,              -- 以下三项由代码按选题记录数出来
    resume_count      int         NOT NULL,
    interviewer_count int         NOT NULL,              -- 只有人数,不存是谁
    source_refs       jsonb       NOT NULL DEFAULT '[]', -- 题引用 id,不存题文
    status            text        NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'approved', 'retired')),
    prompt_version    text        NOT NULL DEFAULT '',   -- 归纳用的 prompt 版本
    reviewed_by       int,
    reviewed_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);
```

题目文本进索引前过脱敏;`source_refs` 只存题引用 id,不存题文。

### 5.1 给前端的契约(题库抽屉 / 打分工作台)

P2 **不新增接口,不新增权限点**。变化只是两个已有接口的返回里多了可选字段;后端两个开关都
关着时(默认),`pickable[]` 与现在完全一样,信封里只是每条仓线多一个值为 `null` 的 `review` 键。

**`GET /admin/evaluation/qbank?resume_id=&cycle_id=`**(权限不变:`interview:evaluate` 或
`resume:audit`)

`pickable[]` 的每一条可能多出两个键。没有标注的题**不带**这两个键,旧题库整体都没有:

| 键 | 形状 | 含义 |
|---|---|---|
| `review` | `{"verdict": "keep" \| "revise" \| "drop", "reasons": string[], "revised"?: true}` | 审题意见。`keep` 时 `reasons` 为空。`revised` 只在拦截模式下被打回并重写过的题上出现(P4),其余题没有这个键 |
| `similarity` | `{"duplicate_of_ref_id": string \| null, "generic_resumes": number}` | `duplicate_of_ref_id` 是同一个 `pickable[]` 里另一条的 `ref_id`——本包里问同一件事、证据更强的那道;`generic_resumes` 大于 0 表示本周期有这么多份其他简历出过近似的题 |

`reasons` 的取值是固定的六个:

| 取值 | 含义 |
|---|---|
| `searchable` | 搜一下就能答,和候选人的项目无关 |
| `ungrounded` | 题目声称的事实在材料里找不到 |
| `recitable` | 背八股就能答,问不出是不是自己做的 |
| `duplicate` | 与同组里另一道题问的是同一件事 |
| `generic` | 换一份简历照样能问 |
| `tone` | 质问、设套、让人难堪的措辞 |

一条线有没有审过、审得成不成,看信封里的线级状态:仓线在
`envelope.groups[i].qbank_v2.review.status`,评测线在 `envelope.groups[i].review.status`,
取值 `reviewed` / `unavailable`;这块不存在或为 `null` 就是没审。`unavailable` 时个别题仍可能
带 `review`(确定性预检给的),其余题没有意见不等于「放行」。

**`POST /admin/evaluation/qbank/pick`**(不变)

请求体不变。把 `pickable` 条目原样传回(带着 `review` / `similarity`)或只传 `ref_id` 加
`question` 都可以,服务端按 `ref_id` 绑定。落进选题记录的是服务端的权威条目,带着勾选当时的
`review` / `similarity`——以后拿选题数据检验审题者,靠的就是这份快照。所以
`GET /admin/evaluation/qbank/picks` 返回的 `question_ref` 里也会出现这两个键。

**探索计划(P3)**:`envelope.plan` 始终存在,形状见 §5。它是给管理面看的运行信息,不影响
选题:每条仓线的档位(`tier`:`high` / `normal` / `low` / `dropped`)、侧重类别、给了多少预算、
实际用了多少。可以在题组标题旁显示档位;`dropped` 的仓在 `groups` 里没有对应的组,要提示
「这个仓因整包预算上限没有探索」只能从 `plan.lines` 里读。不展示也不影响任何现有功能。

**题型管理(P4,三个新接口)**

都在 `/admin/evaluation/exemplars` 下。题型范例没启用(默认)时,列表照常可读,两个写接口
返回 409。**没有新增权限点**:读用 `resume:audit`,写用 `evaluation:run`(题型会进以后每位
候选人的出题 prompt,所以要的是初筛执行权而不是查看权)。

`GET /admin/evaluation/exemplars?status=&cycle_id=`(权限 `resume:audit`)

- 查询参数都可选:`status` 取 `proposed` / `approved` / `retired`;`cycle_id` 是题型归纳自哪个周期。
- 响应 `200`:`{"enabled": boolean, "total": number, "items": Exemplar[]}`。`enabled=false`
  表示题型范例没启用,写接口现在不可用。`items` 按选用面从广到窄排。
- `Exemplar`:

  | 字段 | 类型 | 含义 |
  |---|---|---|
  | `exemplar_id` | number | 题型 id |
  | `cycle_id` | number | 归纳自哪个周期的选题记录 |
  | `category` | string | 题类(十个题类之一) |
  | `pattern` | string | 题型文字:去标识的提问方式。草稿没有经过人审,可能还带着项目细节——审的就是这个 |
  | `pick_count` / `resume_count` / `interviewer_count` | number | 被选了几次、涉及几份简历、几位面试官 |
  | `source_refs` | string[] | 支持这个题型的题引用 id(与 `pickable[].ref_id` 同源),没有题文 |
  | `status` | string | `proposed` 草稿 / `approved` 已通过(会被出题段用作范例)/ `retired` 已停用 |
  | `grade_band` | string | 恒为空串,保留字段 |
  | `prompt_version` | string | 归纳用的 prompt 版本 |
  | `reviewed_by` / `reviewed_at` | number \| null / string \| null | 谁在什么时候审的;没审过为 null |
  | `created_at` | string | 归纳时间 |

`POST /admin/evaluation/exemplars/propose`(权限 `evaluation:run`)

- 请求体:`{"cycle_id": number}`。从这个周期的选题记录归纳题型,写成草稿,**替换该周期原有
  的草稿**(已审过的不动)。会调一次模型,耗时在秒到几十秒。
- 响应 `201`:`{"status": "proposed" | "no_picks", "picked": number, "proposed": number,
  "exemplar_ids"?: number[], "rejected": Rejected[]}`。`picked` 是参与归纳的题数;`no_picks`
  表示这个周期没有可归纳的选题记录,没有调模型。`Rejected` 是
  `{"reason": string, "category": string, "picks": number, "resumes": number,
  "interviewers": number}`——被代码拒收的题型只给原因与计数,不给题型文字。
- 错误:`409` 没启用;`503` 审计服务不可用,**没有执行**,可以重试;`502` 归纳失败(模型调用
  失败或输出不合规),原有草稿没有变动;`403` 没有权限。

`POST /admin/evaluation/exemplars/{exemplar_id}/review`(权限 `evaluation:run`)

- 请求体:`{"decision": "approve" | "retire"}`。`approve` 让这条题型开始被出题段用作范例,
  `retire` 停用。
- 响应 `200`:`{"exemplar_id": number, "status": "approved" | "retired"}`。
- 状态只能往前走:草稿→通过 / 停用,通过→停用。不允许的变更返回 `409`(`detail` 里写着
  现在的状态),通常是别人刚处理过,刷新列表即可。
- 其他错误:`409` 没启用;`404` 没有这条题型;`503` 审计服务不可用,**状态没有改动**,可以
  重试;`422` `decision` 不是那两个取值;`403` 没有权限。

人审时要看的是:题型文字里还有没有能认出具体候选人或具体项目的东西(项目名、人名、学校、
数字)。代码的闸只拦得住网址、文件名、路径、手机号这类有形状的东西。

**展示上要注意的两点**

- P2 是只标注:题目一道不少,前端不要默认按意见隐藏或过滤题目。
- 要不要默认展示给面试官还没有定(§12 第 3 条)——展示会影响选题,进而污染对审题者的检验。
  建议把这层展示做成可以整体关掉的。

### 5.2 给周期复盘的数据出口

只读、纯代码、只出聚合计数。复盘线直接调函数,不经 HTTP:

```python
from official_agent.state.qbank import pick_type_summary

def pick_type_summary(cycle_id: int) -> dict[str, Any]: ...
```

同步函数,读 `qbank_pick_log` 与 `qbank_exemplar`(在 async 上下文里用 `asyncio.to_thread` 调)。
返回值里**没有题文,也没有任何面试官或简历的 id**:

```jsonc
{
  "cycle_id": 2026,
  "total_picks": 37,      // 去重后的选择次数:同一位面试官重复勾同一道题只算一次
  "resumes": 12,          // 被选过题的简历数
  "interviewers": 4,      // 选过题的面试官人数
  // 以下四个切面各自按 picks 降序;每行都有 picks / resumes / interviewers 三个计数
  "by_category":       [{"category": "C4_实现细节拷打", "picks": 9, "resumes": 6, "interviewers": 3}],
  "by_role":           [{"role": "chain", "picks": 20, "resumes": 10, "interviewers": 4}],
  "by_group_kind":     [{"group_kind": "repo", "picks": 28, "resumes": 9, "interviewers": 4}],
  "by_review_verdict": [{"verdict": "unreviewed", "picks": 30, "resumes": 11, "interviewers": 4}],
  // 该周期归纳出的题型及其计数;题型文字只有已通过人审的才给,草稿与停用的为 null
  "exemplars": [
    {"exemplar_id": 7, "category": "C2_技术选型与权衡", "status": "approved",
     "pattern": "让候选人说明某次更换实现方案的触发条件", "picks": 5, "resumes": 3, "interviewers": 2}
  ]
}
```

- `by_category` 的取值:仓线与简历线的题是十个题类;评测线、奖项线、兜底线的题是它们自己的
  锚点名(如 `guided`)。`by_review_verdict` 是题被选时带着的审题结论(`keep` / `revise` /
  `drop`),没审过的记 `unreviewed`——拿选题数据检验审题者准不准,看的就是这一项。
- 题型范例没启用、或题型表读不到时,`exemplars` 是空列表,其余照常返回。这个函数不受任何
  开关控制:选题记录一直在记。
- 聚合的纯函数是 `summarize_picks(picks: list[dict[str, Any]]) -> dict[str, Any]`(同一模块),
  不碰数据库,测试里可以直接喂合成的选题记录。

## 6. 角色

| 角色 | 形态 | 看得到 | 产出 | prompt |
|---|---|---|---|---|
| 规划者(新) | 单次调用,无工具 | 候选线的元信息、项目文本 | 每线的档位与侧重 | `evaluation/plan.md` |
| 探索者(已有) | 受限循环,只读 GitHub 工具 | 一个仓 | 档案 | `evaluation/explore.md`,不变 |
| 出题者(已有) | 单次调用 | 一条线的档案 | 题组 | `evaluation/grilling.md` 等,增加范例段 |
| 审题者(新) | 单次调用,无工具 | 一条线的题组与档案 | 逐题意见 | `evaluation/question_review.md` |
| 题型归纳者(新,P4) | 离线单次调用,无工具,管理员触发 | 一个周期里被选用过的题(已脱敏) | 去标识的题型草稿 | `evaluation/exemplar_induce.md` |

出题者在 P4 多了两段可选的附加 prompt,都接在原 prompt 的后面、原文件不动:打回重写
(`evaluation/question_revise.md`,只在拦截模式下)和范例段(`evaluation/exemplars.md`,
只在题型范例打开且有已通过的题型时)。

## 7. 安全与失败语义

失败语义见 D10;这里只列安全属性。

- 仓内容、简历、往届题型都是不可信输入:工具返回过 `guard_tool_result`(已有),进 prompt 包
  数据区。
- 规划者的输出只在代码定义的枚举里取值,注入最多让某条线降档,不能加预算、加线或换工具。
- 审题者看到的题目是模型产出、与仓内容同源,同样包数据区;它的意见只在固定集合里取值。
- 预算封顶在代码里,这是出题线最重要的一条安全属性:候选人能影响的只有「有几条候选线」,而那
  已经被 `MAX_REPO_LINES` 封住。

## 8. eval 与门禁

| suite | 性质 | 断言 |
|---|---|---|
| `qbank_probes`(已有) | 真模型 | 结构与证据性质不退化 |
| `qbank_judge`(已有) | 真模型 | 四维评分不低于基线 |
| `cv_dive_report`(已有) | 人工评审 | 简历深挖质量 |
| `plan_probes`(新,已写;确定性段**已通过**,模型段**未跑**) | 确定性 + 真模型 | 任何输入下预算上限不被突破;fork 与教程仓落在低档;规划者失败时退回默认 |
| `question_review_probes`(新,已写,**未跑**) | 真模型 | 植入的坏题(可搜、无出处、通用、措辞不当)被标出;历史上被选用的好题不被判 `drop` |

并行化(P1)的验收不需要新 suite:同一批简历上题库质量指标不降,整包墙钟下降,token 持平。

落地时发现,上表已有的三个 suite **都不经过 `run_bundle`**:`qbank_probes` 喂的是校验器
(确定性,不调模型),`qbank_judge` 评的是固定题组,`cv_dive_report` 直接调调查子图。所以它们
通过只说明单线的出题与校验没有退化,测不到编排层。P1 的验收因此分两层:

- **编排行为**由单测覆盖(`tests/test_b4_bundle.py`、`tests/test_github_tools.py`):各线确实
  同时在跑、仓线并发度封顶、单线失败不影响在途的其他线、汇合处只留第一条简历深挖且用量不
  漏记、GitHub 在途请求数封顶。
- **墙钟与质量**要拿同一批多仓简历在改动前后各跑一遍真实初筛:墙钟看 runner 日志里
  `eval_event stage=qbank_done` 的 `duration_ms`;token 看 `agent_conversation_log` 里
  `channel='evaluation'` 的四列用量;质量看 `qbank_judge` 与 `cv_dive_report` 的人工评审。
  只想看编排本身能省多少墙钟,可以跑 `evals/benchmarks/bundle_wallclock_benchmark.py`
  (各线耗时为模拟值,不调模型)。

`question_review_probes` 的现状:数据集(`evals/datasets/question_review_probes.yaml`)与执行器
都已写好并进了 runner 的注册表,**没有在真实模型上跑过**——开发机没有模型 key,runner 如实报
SKIP。它出三个指标:

| 指标 | 含义 | 门槛(初值) |
|---|---|---|
| `planted_flag_rate` | 植入的坏题被标出来、且理由对得上的比例 | ≥ 0.7 |
| `good_not_dropped_rate` | 好题没有被判 `drop` 的比例 | = 1.0 |
| `good_keep_rate` | 好题被判 `keep` 的比例,即没有误伤 | 只记录;开拦截模式之前看它 |

三点要老实说明:数据集里的「好题」是手写的,不是历史上被选用过的题(还没有选题数据可取);
门槛是拍的,第一次跑出分布之后要重定并写基线;植入的坏题只有 7 道,够做冒烟,不够下结论。
题目索引没有对应的 suite:它不调模型,规则由单测覆盖,要校准的是门槛(§12 第 2 条)。

`plan_probes` 的现状(`evals/datasets/plan_probes.yaml`):

- **确定性段已经跑过并通过**(13 条,不调模型,任何环境都跑):整包封顶在各种档位组合与上限下
  不超限、砍线的先后符合规则;候选集合之外的线进不了计划;集合外的档位、类别、想自己定预算
  或加工具的输出被拒收。这一段任何一条不过 suite 就 FAIL,分布模式也不放行。
- **模型段没有跑过**(3 条:fork 与教程仓落在低档而主项目最高档;仓描述里的注入文本抬不高
  档位;写了具体贡献的他人项目不是低档)。没有 LLM 配置时逐条记为跳过、不计分,备注里写明
  「PASS 只代表确定性段」。门槛(通过率 1.0)是初值,3 条用例只够冒烟。
- 「规划者失败时退回默认」由单测覆盖(`tests/test_plan.py`),不在数据集里。

P4 的两项:

- **拦截模式没有单独的 suite**。改题组的动作(换题、丢题)是确定性的,由单测覆盖;该不该开
  拦截,看的是 `question_review_probes` 的 `good_keep_rate`——还没有跑过,也还没有定门槛。
- **`exemplar_probes`**(`evals/datasets/exemplar_probes.yaml`,新):
  - 确定性段**已经跑过并通过**(10 条):题型文字的闸、计数与选用面门槛。
  - 模型段**没有跑过**(1 条:归纳结果里不能留着原题埋的项目名、文件名、人名、学校名和数字,
    每条都要过闸,且至少归纳出一条够门槛的题型)。素材是合成的,不是真实选题记录。

## 9. 成本与预算闸

- P1 不增加调用,只缩短墙钟。例外是 D1 记下的那一种:多条仓线同时回退到简历深挖时,多出来的
  几次出题调用在汇合处被丢弃。并发抬高的是峰值而不是总量(峰值见 D1)。
- 规划者每份简历多一次小调用(只在打开、且至少有一个可读的仓时);它省下的是低档线上的
  探索轮数,净效果预期是省。规划调用的用量计入 `explore_usage_total`。它排在起线之前,是整包
  墙钟上新增的一小段串行时间。
- 审题者每条过审的线多一次 strong 调用,输入是档案加题组。原设想是「档案是出题时刚用过的
  前缀,可以命中缓存」,落地时发现做不到(D5)。审题在线内排在出题之后,所以抬高的是总量,
  不是并发峰值;它的用量与探索、出题一起计入 `explore_usage_total`。
- 题目索引每份题库多一次批量 embedding 调用和几十条短查询,不调对话模型。
- 拦截模式(P4)下,有题被打回的线再多一次出题调用和一次审题调用。
- 题型归纳(P4)是管理员手动触发的离线调用,一个周期一次到几次;范例段让每次出题的输入多
  几百个字,不增加调用次数。
- 整包预算硬上限写在配置里(`QBANK_EXPLORE_MAX_TOTAL_TURNS` /
  `QBANK_EXPLORE_MAX_TOTAL_WALL_SECONDS`),规划者、候选人都改不了。缺省值不比今天紧(D2)。

## 10. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| P1 | 现有证据线并行化;简历深挖在汇合处去重;GitHub 并发闸 | 多仓简历的整包墙钟明显下降,质量指标不降 |
| P2 | 审题者(只标注)+ 题目索引(包内查重、通用题标注) | `question_review_probes` 通过;标注随题库可见 |
| P3 | 规划者与分档预算 | `plan_probes` 通过;探索总轮数下降而 judge 评分不降 |
| P4 | 审题拦截模式;选用题型范例 | 需要至少一个周期的选题数据;误杀率达标后才开拦截 |

**进度**:P1 已实现;P2、P3、P4 的机制已实现、默认关闭、**验收未完成**。四期的
`ruff check` 与全量单测都通过。

P1 没有做完的验收与留下的尾巴:

- **真实模型下的墙钟、token 与质量对比没有跑**(开发机没有模型 key;方法见 §8)。模拟耗时下,
  5 条仓线加评测线的整包墙钟约为逐条跑的 1/2.6——这只说明编排,不代表真实数字。
- 子图里的 `cv_dive_done` 参数在生产路径上已经没有调用方传 `True`(§12 第 8 条)。

P2 做了什么:审题者(只标注)、题目索引(包内跨线查重与跨简历通用题,只标注)、评估域模型
并发闸(D1)、`question_review_probes` 数据集与执行器、给前端的契约(§5.1)。

P2 **不能算验收通过**,原因和打开之前要做的事:

- `question_review_probes` 没有跑过。审题的 prompt 一次真实模型调用都没有经历过,所以
  `QBANK_REVIEW_MODE` 默认是 `off`。打开的顺序:有 key 的人先跑这个 suite(先用
  `--distribution` 看分布),按分布重定门槛并写基线,达标后再把配置改成 `annotate`。
- 相似度的三个数没有校准,也没有回填旧题库的工具(D7),所以 `QBANK_INDEX_ENABLED` 默认是关。
  打开之前先写回填脚本、量分布、重定门槛。
- 「标注随题库可见」只做到了接口:返回里有字段,前端还没有展示(另一个仓)。
- 评估域模型并发上限 8 是初值,没有对着模型供应商的限流实测过。

P3 做了什么:探索双闸改成按线传入的预算、三档预算的映射、整包封顶(始终生效)、规划者
(默认关闭)、计划与实际用量进信封、`plan_probes` 数据集与执行器。

P3 **不能算验收通过**,原因和打开之前要做的事:

- `plan_probes` 的模型段没有跑过,规划者的 prompt 一次真实调用都没有经历过,所以
  `QBANK_PLAN_MODE` 默认是 `off`。打开的顺序:有 key 的人先跑这个 suite,达标后再改成 `plan`。
- 验收写的是「探索总轮数下降而 judge 评分不降」。两件事都要真实模型:前者看打开前后信封里
  `plan.lines[].turns_used` 的合计,后者看 `qbank_judge`。都没有做。
- `normal` / `low` 的预算数值没有校准。规划者关着的这段时间,信封里已经在记每条线实际用了
  多少轮,够一个周期之后按分布重定(§12 第 1 条)。

P4 做了什么:审题拦截模式(打回的题由出题者改一次并复审,建议丢弃的题在不破坏结构时丢弃)、
题型范例(归纳、人审、取用,以及管理面的三个接口)、给周期复盘的选题汇总函数(§5.2)、
`exemplar_probes` 数据集与执行器。

P4 **验收不了,两项都不能打开**——用户知道这一点,要的是机制先做好:

- 拦截模式的启用条件是审题者的误杀率达标。`question_review_probes` 还没有跑过,门槛也还
  没有定。在那之前不要把 `QBANK_REVIEW_ENFORCE_READY` 置真。
- 题型范例的启用条件是至少一个完整周期的选题数据。现在没有:归纳、人审、取用都只用合成
  数据测过机制;归纳用的 prompt 没有在真实模型上跑过(`exemplar_probes` 的模型段没跑)。
- 题型范例对出题质量有没有帮助,完全没有验证过,也没有设计对照实验。
- 管理面的题型审核页还没有(前端,契约见 §5.1)。

## 11. 代码落位

- `evaluation/bundle.py`:串行循环改并发,汇合处去重(P1 已落地);`evaluation/explore.py` 的
  双闸从模块常量改成参数(默认值不变,P3 已落地)。
- `evaluation/plan.py`(P3 已落地):候选线枚举、档位到预算的映射、封顶——纯函数,可单测;
  规划者调用也在这个模块里。`evaluation/explore.py` 的双闸已改成按次传入的 `ExploreBudget`。
- `evaluation/question_review.py`(P2 已落地):确定性预检 + 审题调用;审题段接在调查子图的
  出题之后(`investigate_graph.review_node`)和评测线上(`bundle._autograding_line`)。
- `evaluation/question_similarity.py`(P2 已落地,原稿没有列):相似度标注的编排——算向量、
  跨线查重、查通用题、落索引;由 `runner._do_qbank` 在题库落库前后各调一次。
- `state/qbank.py`:可挑题视图并入审题意见与相似度标注(P2 已落地;信封没有升 v3,见 §5);
  `state/qbank_index.py`(P2 已落地):题目索引。题型表留给 P4。
- `evaluation/llm_common.py`:评估域模型并发闸 `model_gate` / `gated_ainvoke`(P2 已落地)。
- 拦截模式(P4 已落地):改题组的两个纯函数在 `evaluation/question_review.py`;重写与复审的
  编排在 `investigate_graph._enforce`。
- `evaluation/exemplar.py`(P4 已落地,原稿没有列):题型的归纳、入库前的闸、取用;
  `state/qbank_exemplar.py`(P4 已落地):题型表;`web/evaluation_admin.py`:题型管理的三个接口。
- `state/qbank.py`:`pick_type_summary` / `summarize_picks`,给周期复盘的只读数据出口(P4 已落地)。
- `evaluation/github_client.py`:请求并发上限(P1 已落地)。
- `prompts/evaluation/plan.md`、`prompts/evaluation/question_review.md`,以及对应的 eval 执行器
  (`question_review.md` 与 `evals/question_review_probes.py` P2 已落地;`plan.md` 与
  `evals/plan_probes.py` P3 已落地;`question_revise.md`、`exemplar_induce.md`、`exemplars.md`
  与 `evals/exemplar_probes.py` P4 已落地)。

## 12. 开放问题

1. 档位的三组双闸数值是拍的,要用 P1 上线后的真实用量分布校准(多少线其实在 20 轮内就结束了)。
   数据从 P3 起记在信封的 `plan.lines[]` 里(`turns_used` / `seconds_used`),规划者关着也记。
2. 包内查重与通用题检测的相似度门槛,要先用现有题库离线量一遍分布。
3. 审题标注要不要展示给面试官,还是只进数据——展示会影响选题,进而污染对审题者的检验。
4. (已定)题型归纳在本提案里做;周期复盘通过只读函数 `pick_type_summary` 取聚合计数(§5.2),
   不自己读选题记录,也不调归纳模型。
5. 通用题检测跨不跨周期:跨周期能发现「年年都问」的题,但要长期保留题目索引。
6. 仓线并发度 3 与 GitHub 请求闸 4 都是按「峰值不比并发前高太多」拍的,要用上线后的限流与
   降级日志校准(`bundle_cv_dive_dedup`、探索降级原因、模型侧 429)。
7. (已定,见 D1)排队等评估域模型并发闸的时间**不算**进探索的 300 秒墙钟。留下的问题是
   整包墙钟在忙时没有硬上限,要不要给题库段加一个总超时。
8. 子图的 `cv_dive_done` 参数要不要删:逐条跑线的调用方已经没有了,留着是为了不动子图路由。
9. 审题意见与相似度标注在同一道题上打架时(审题者放行,索引说通用)前端怎么合并展示,以及
   P4 的拦截模式以谁为准。
10. 通用题检测只看得到「已经入索引的简历」:一个周期里最早评的那批简历永远没有可比对象。
    要不要在周期中段对全部题库重算一遍标注。
