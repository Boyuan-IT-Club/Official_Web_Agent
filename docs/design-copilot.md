# C 模块设计:面试官 Copilot

> v1.0 · 2026-09-11。范围 = design.md 第 2 节「C · 面试官 Copilot」的实施方案,
> 对应 issue COP-01..08(#38~#45)。与 design.md/ADR 冲突时,以本文档的「关键决策」
> 一节为准(其中三条修正了 design.md 写于 2026-08 的过时判断)。

## 1. 范围与目标

面试官在管理端面试页得到三件事:**面试前**一页候选人卡片(简历摘要 + B 模块预置题库);
**面试中**基于对话进展的低延迟追问建议;**面试后**按评价维度归纳的评价表草稿。

**非目标**:不替面试官做判断(草稿必须人确认才落地);不做实时语音答案生成
(那是候选人侧作弊工具的形态,与本模块无关);v1 不做面试官个性化建议。

## 2. 关键决策

### D1 · 三态状态机,不用 ReAct(ADR-0003)

A 模块用 ReAct 单循环是因为输入是开放式自然语言、**意图未知**;C 的宿主界面本身
就知道面试官是谁、在面谁、处于哪个阶段——**界面即路由**。让模型再推断一次意图既
费 token 又引入不确定性。故 C 用原生 `StateGraph`,由代码按 `state` 字段条件路由。

三态语义:

| 状态 | 输入 | 输出 | 工具调用 |
|---|---|---|---|
| 准备中 prep | schedule_id | 候选人卡片 + 题组 | 有 |
| 面试中 live | 速记/转写流 | 追问建议 | **零** |
| 整理中 wrap | 全程记录 | 评价草稿(按维度) | 有 |

「面试中零工具调用」是延迟契约,不是优化项:所需上下文在 prep 态一次性装进 prompt。

### D2 · 宿主复用 EvaluationWorkspace,不新建页面

`Official_Web_Frontend/src/pages/EvaluationWorkspace/` 已是成熟独立页面(左简历 /
右逐维度评价 / FilmStrip 切换同场候选人 / 协同 Y.Doc 写入)。C 模块是往里加一个
Copilot 面板。这也与 B 模块 B7 规划的「打分工作台题库抽屉」同一落点。

### D3 · 不做说话人分离,整段转写交给模型 ★

**修正 design.md**:COP-06 原写「说话人分离(面试官 vs 候选人)」。经评估取消。

理由是**容错代价不对称**:候选人侧作弊工具必须精确知道「哪句是面试官问的」,因为
输出要照着念,标签错=当场失败;而本模块的输出是给面试官参考的,他本人完全知道刚才
发生了什么,一条不合适的建议成本接近零。

同时 LLM 对问答结构的**语义推断**(疑问句式、话轮长度、填充词)在嘈杂线下房间里
很可能比声纹聚类更准。配合 ASR 自带的时间戳与停顿标注(免费信息,长停顿是话轮切换
强信号),推断质量足够。

**连带消失的复杂度**:双麦克风、能量比较、声纹注册(3D-Speaker/CAM++)、
线上/线下架构分叉——全部不需要,单路音频一条代码路径。

**代价与补偿**:评价草稿可能误把面试官的话当候选人表述。补偿措施见 D6
(草稿必须附原文依据),用可核对性替代准确性保证——与 B1 评分卡「逐字引用原文句」同源。

### D4 · 触发:手动为主,静默兜底

取消说话人标签后,「候选人说完一段」这个触发判据不再成立。改为双轨:

1. **手动(主路径)**:面试官点按钮/敲快捷键。他最清楚何时需要建议,零误触发;
   而且这个动作本身就是最准的「该出建议了」信号。
2. **静默(辅助)**:静默超过 5s 自动出一次,节流 30s 最多一次。宁可少给。

### D5 · 会话场次级共享,不按面试官分

线下一场有多位面试官。若每人各开一个会话,就是 N 路重复音频、N 份 ASR 账单、
N 个可能不一致的转写。故:

- `thread_id` = `copilot:s{schedule_id}`(**场次级**,不带面试官 id)
- 属主校验 = 「是该场次的面试官之一」(后端场次绑定关系已能验证)
- 一份建议流,多个 SSE 订阅者;任一面试官的速记,同场次都能看到

理由:面试是集体行为,三人听到的是同一段话,「可以追问什么」对谁都一样。
个性化留待 badcase 证明必要时再做。

### D6 · 评价草稿由前端代写进 Y.Doc,且必须附原文依据

后端 `IEvaluationBoardService` 注释写明「编辑期的真源是协同服务持有的 Y.Doc,
本服务不碰单元格内容」。agent 直连协同服务实现 Y.Doc 协议不划算,故:

```
agent 产出结构化草稿 → SSE 推前端 → 面试官编辑确认 → 前端调 CollabTextArea 写 Y.Doc
```

这同时满足 ADR-0005「agent 只加速流程、不替人拍板」。草稿 schema 必须携带 evidence:

```json
{ "dimension": "技术深度",
  "draft": "对分布式锁有基本认知,但未考虑 Redis 故障场景",
  "evidence": ["我用了 Redis 做分布式锁", "呃…这个我确实没考虑过"] }
```

### D7 · 取数走面试官本人 JWT,SEC-01 不是阻塞

**修正 design.md**:原写「get_candidate_card 依赖 X-On-Behalf-Of 落地,服务账号
直调会被场次绑定校验拒绝」。实际后端 `InterviewEvaluationController.candidateResume`
用 `SecurityUtil.getCurrentUsername()` 按**当前登录用户**的场次绑定放行,面试官持
`interview:evaluate` 即可。配合 #143 落地的 `asker_scope`(本人 JWT 裸发),开箱可用。
前端 EvaluationWorkspace 已在使用该端点。

### D8 · checkpointer 用 Postgres

**修正 issue #41 票面**「checkpointer 落 Redis」——ADR-0007 已将 Redis 移出 agent 栈。

## 3. 架构

```
┌─ EvaluationWorkspace(浏览器)──────────────────────────────┐
│  左:简历速览   中:逐维度评价(Y.Doc)   右:Copilot 面板 ←新增│
│  音频(阶段二):单路 getUserMedia/getDisplayMedia            │
└──────────────┬─────────────────────────────────────────────┘
               │ 三个 POST 端点 + SSE(复用 #90 契约)
┌──────────────▼─────────────────────────────────────────────┐
│ web/copilot_routes.py   鉴权 → 身份解析 → 场次属主校验       │
├────────────────────────────────────────────────────────────┤
│ 输入源适配层   速记(手打) │ ASR 转写流                      │
│                ↓ 统一为 Utterance{text, ts, is_final, pause}│
├────────────────────────────────────────────────────────────┤
│ 注入防御(#163)  转写/速记都是不可信输入                     │
├────────────────────────────────────────────────────────────┤
│ 触发闸门 gate.py   手动信号 / 静默窗口 / 节流 / 并发闸       │
│                    纯函数、确定性、不调模型                  │
├────────────────────────────────────────────────────────────┤
│ graphs/copilot/    prep / live / wrap 三态图                │
│                    三层 prompt(stable / 备忘 / fast)       │
├────────────────────────────────────────────────────────────┤
│ 复用层  asker_scope · identity · threads · checkpointer     │
│         pii · injection_guard · prompt_loader · 用量埋点     │
└──────────────┬─────────────────────────────────────────────┘
               │
   后端 REST(面试官本人 JWT)· Agent PG · qbank(B 模块产出)
```

`Utterance` 是整个设计的枢纽:阶段一速记直接构造它,阶段二 ASR 产出它;
闸门与三态图只认这个结构,**上游换什么都不用改**。

## 4. 数据面

新表 `copilot_session`(Agent PG,与 checkpointer 同库):

```
session_id(=thread_id) / schedule_id / cycle_id / cycle 内候选人标识
/ state(prep|live|wrap|done) / memo TEXT / created_at / updated_at
```

- thread_id 遵守 ADR-0008:`copilot:s{schedule_id}`
- 速记/转写与建议进 **checkpointer**(按 thread_id),断线换设备不丢(COP-04 诉求)
- 属主校验复用 `state/threads.resolve_thread()`,判据换成场次绑定
- **备忘单独存列**而非只在 checkpoint:wrap 态要整份读取,管理端可能要查;
  checkpoint 是 SDK 内部 schema,不适合当查询面(与 M6 用 conversation_log 同理)

## 5. Prompt 三层布局(ADR-0004)

```
stable prefix(字节级稳定,吃 prefix cache)
  = 面试官 persona + 候选人卡片 + 题组摘要 + 评价维度定义
slow state(单独一条消息:刷新只失效此点之后的缓存)
  = 滚动备忘
fast context
  = 最近 N 分钟转录(带时间戳与停顿标注)+ 手动触发时的面试官指示
```

**滚动备忘四节**(≤800 字,代码 clamp 1000;异步更新,离开关键路径):

```
【已问问题】       每条一行,最新在最后
【已出现的事实声称】数字/经历/技术选型,由模型判断归属并标注;用于发现前后矛盾
【待深挖点】       提到但没展开的
【风险信号】       与简历不符 / 回避 / 深度不足
```

第二节是与候选人侧工具的镜像差异:他们用它防止自己矛盾,**面试官用它发现矛盾**,
正好接上 B 模块 `claims_vs_reality` 证据锚。

**prefix cache 预热**:进入 live 态时先发 `max_tokens=1` 的预热请求,system 与真实
请求字节相同,把缓存打热(#113 的命中率埋点可验证效果)。

## 6. 触发闸门(graphs/copilot/gate.py)

纯函数、可单测、不调模型。判据顺序:

```
手动信号?            → 直接放行(跳过下面全部)
静默 ≥ 5s?           → 继续
距上次建议 ≥ 30s?    → 继续
缓冲区有效内容 ≥ N 字?→ 继续
并发闸有位?(复用 #194) → 放行,否则回 busy 不排队
```

面试中宁可这一句不给建议,也不要 30 秒后吐出一条过时的追问。

## 7. 三段数据流

**准备中**:前端 POST `/api/copilot/prep {schedule_id}` → JWT 换身份 → 建/取
场次 thread → `asker_scope` 内三路并行取数(候选人简历 / qbank 题组 / 评价维度)
→ 组装并冻结 stable prefix → 预热 cache → SSE 返回卡片与题组。

**面试中**:速记或转写进缓冲 → 闸门判定(不放行则直接返回,零模型调用)→ 构造三层
prompt → **一次模型调用**流式出建议 → SSE 吐出 → 面试官勾选进 pick log(复用
`/admin/evaluation/qbank/pick`,反哺 B 出题)→ **返回后**异步更新备忘(注意持
asyncio 任务强引用,防 GC,B2 评审踩过此 P1)。

**出题来源**:题组全量(≤15 题,含三锚参考答案与 evidence,约 3000 字)在 prep 态
写入 stable prefix——整场字节不变,命中 prefix cache 后近乎零成本。故 live 态**不做
独立检索步骤**:模型在同一次调用里既能引用预置题(带 evidence.path,面试官可点开
对应仓文件),也能基于当前回答现场生成追问。要求输出标注来源(`qbank:{qid}` /
`generated`),供 pick log 归因与 B 模块反哺。

模型看得见全部题目再结合语境挑选,质量优于关键词检索;省掉检索层也少一处失败面。

**整理中**:读全程记录 + 备忘 → 按维度模板归纳,strict Pydantic 结构化输出(沿用 B1
的「提示词 JSON + schema 校验」轨,因代理模型拒 json_schema)→ SSE 推带 evidence 的
草稿 → 前端展示 → 面试官确认 → 写 Y.Doc。

## 8. 分阶段与票

### 阶段一:速记版(主体,不碰音频)

| 票 | 内容 | 依赖 |
|---|---|---|
| C1 | 三态图骨架 + copilot_session 表 + 场次级 thread 契约 | — |
| C2 | 候选人卡片(COP-01):本人 JWT 取简历 + qbank + 维度 | PR #165 |
| C3 | 触发闸门 + 追问建议(COP-02):三层 prompt / 备忘 / 混合模式 / 零工具 | C1,C2 |
| C4 | 评价草稿(COP-03):按维度归纳 + 必带 evidence,SSE 推送 | C3 |
| C5 | 前端 Copilot 面板:消费 SSE,草稿一键写入 CollabTextArea | C2~C4 |
| C6 | 场次汇总(COP-05) | C4 |
| C7 | eval 覆盖:追问质量探针 + 草稿 schema 断言,进 #148 runner | C3,C4 |

### 阶段二:语音版

| 票 | 内容 | 备注 |
|---|---|---|
| C8 | 录音合规(COP-08) | design.md 要求**先行**,不可跳 |
| C9 | 浏览器音频采集:单路 + AudioWorklet + PCM16 + WS 上行 | 纯前端,可与阶段一并行 |
| C10 | ASR 适配层:带标点引擎 + 幻觉过滤 + partial 合并 → Utterance | 纯后端 |
| C11 | 接线验收:转写流替换速记输入源,**图与闸门零改动** | 验证枢纽抽象 |

C9 纯前端、C10 纯后端,**两人可完全并行**。

## 9. 复用与新增

**直接复用(0 行改动)**:`tools/client.py` + `asker_scope`(#143)、
`graphs/identity.py`、`state/threads.py`、`state/pg.py`、`security/pii.py` +
`injection_guard.py`(#163/#164)、`prompt_loader.py`、`observability.py`、
`state/conversation.py`(用量埋点)、SSE 契约(#90)。

**新增**:

```
graphs/copilot/{__init__,gate,memo,suggest}.py
state/copilot.py
web/copilot_routes.py
prompts/copilot/{prep,live,wrap}.md
```

## 10. 风险与边界

- **基线**:PR #165(107 文件)与 #194(37 文件)未合且互相冲突。C 模块新增文件
  与两者几乎不重叠,C1 可立即基于 main 开工;C2 需等 #165 合并(消费 qbank)。
- **转写是新的不可信输入面**:候选人当场念「忽略以上指令」与简历注入同类,
  必须过 #163 守卫。
- **ASR 分句与标点质量是质量下限**:取消说话人分离后,模型靠句法推断话轮,
  无标点的转写会显著降低准确率。故 ASR 选型**必须带标点**(如 Fun-ASR-Nano),
  这从加分项升级为硬需求。
- **ASR 幻觉**:静音时模型会吐训练残留(中文如「谢谢观看」「字幕由…提供」),
  需建黑名单过滤,否则沉默会变成假转写喂进 prompt。
- **partial/final 合并**:流式 ASR 反复吐同句不同版本,需做重叠检测合并;
  参考实现是英文词级的,中文需改为字符级。
- **延迟预算宽松**:面试官看建议可容忍 3~5s(他自己也在思考),比候选人侧工具的
  <1s 宽一个数量级。故**不降档 model_light**,strong + prefix cache,质量优先。
- **失败姿态**:观测 fail-open(trace 挂不上不影响面试);草稿写 Y.Doc 人确认
  fail-closed;题库缺失降级纯生成;prep 未完成进 live 直接拒绝。

## 附录:调研来源与可复用参数

调研对象(2026-09,GitHub):MeetingCopilot(215★,中文/FunASR/Electron)、
transcribe(265★,Python/MIT)、Open-Cluely(151★,浏览器音频管线)、
cue(1.3k★)、cheetah(4.3k★)。**注意这些项目服务候选人侧,产品定位与本模块相反,
仅借鉴工程实现。**

浏览器音频管线参数(多项目一致,阶段二直接采用):

| 参数 | 值 |
|---|---|
| 采样率 | 16000 Hz |
| 发送帧长 | 100ms(最小 50ms) |
| worklet chunk | 2048 samples,ring buffer 4× |
| 编码 | PCM16(Float32→Int16) |

⚠ AudioWorklet 节点只有连到 destination 才会被 pull,须经**零增益 gain** 连接
(不发声但数据可流),否则静默无数据——必踩的坑。

中文 ASR 引擎(FunASR 生态,实测特性来自 MeetingCopilot sidecar):

| 引擎 | 流式 | 中文 | 英文 | 标点 |
|---|---|---|---|---|
| paraformer-zh-streaming(220M) | 真流式 600ms chunk | 好 | 差 | 无 |
| Fun-ASR-Nano-2512(0.8B) | 伪流式(1.2s partial) | 好 | 好 | **有** |

端点检测参数:`SILENCE_FLUSH_MS=700`、`SILENCE_RMS=0.004`。

**架构模式借鉴**:本地 sidecar 可实现与云端 ASR **完全相同的 WebSocket 协议**
(run-task / task-started / 二进制 PCM16 / result-generated / finish-task),
使「开发用本地模型、生产用云端 API」只需改 URL,应用层零改动。
