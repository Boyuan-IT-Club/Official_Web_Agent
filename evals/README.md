# Evals

agent 的"单测"。统一 runner(evals/run_evals.py)执行,分两层 + 专项数据集:

1. **用例断言**(`evals/cases/*.yaml`):给定输入,断言 agent 行为(调了哪些
   工具/参数)——真实 LLM 驱动真实 ReAct 图,后端 HTTP 由 canned fake 顶替。
2. **阈值型门禁**(`evals/datasets/*.yaml`):数据集指标 ≥ 基线,负例不误命中
   ——真实 embedding + 真实检索链,不 mock。
3. **终答质量**(LLM-as-judge,预留):正确性 / 有用性 / 语气。

## 统一 runner

```bash
uv run python evals/run_evals.py                      # 全量
uv run python evals/run_evals.py --suite kb_probes    # 单 suite
uv run python evals/run_evals.py --distribution       # 打分分布(不设门,退出码恒 0)
uv run python evals/run_evals.py --baseline evals/baselines.json      # 低于基线即 FAIL
uv run python evals/run_evals.py --write-baseline evals/baselines.json # 落新基线
```

- 退出码:**0** 全过 | **1** 有 FAIL | **2** 全部 SKIP(环境未配置——门禁不可
  静默变绿)。
- suite 文件顶层 `runner:` 字段声明执行器;新探针(qbank_probes / judge /
  回归用例)落新 kind + `src/official_agent/evals/` 新 executor,引擎不改。
- 基线:metrics 语义统一「越高越好」,`--write-baseline` 生成、`--baseline`
  对比,任何指标低于基线 → REGRESSION → FAIL(合入门禁)。语料/embedding 模型
  变更后需重新校准并重写基线。
- 基线文件里除了 metrics 还有两块:`observations` 是只记录不设门的观测量(成本、耗时,
  方向不是越高越好,不参与对比);`context` 是当时的运行条件(模型、prompt 版本、数据集
  指纹)。数据集指纹与基线不同时,报告里会提示「对比不可靠,应重录基线」。
- SKIP 的 suite 不写进基线(它没有指标);全部 SKIP 时不落文件。

## Suite 一览

| Suite | 面 | kind | 环境要求 |
|---|---|---|---|
| `cases/tool_selection.yaml` | cases | `tool_selection` | LLM_* 配置 |
| `cases/injection_probes.yaml` | cases | `injection_probes` | 无(确定性) |
| `datasets/kb_probes.yaml` | datasets | `kb_probes` | kb.store 可导入 + EMBED_* + 语料入库 |
| `datasets/qbank_probes.yaml` | datasets | `qbank_probes` | 无(确定性) |
| `datasets/qbank_judge.yaml` | datasets | `qbank_judge` | LLM_* 配置 |
| `datasets/cv_dive_report.yaml` | datasets | `cv_dive_report` | LLM_* 配置 + `real_resumes/` 有样本 |
| `datasets/scoring_probes.yaml` | datasets | `scoring_probes` | LLM_* 配置;CD 部署后在生产容器里自动跑(只报告,每份只评一次) |
| `datasets/memory_probes.yaml` | datasets | `memory_probes` | LLM_* 配置(提取闸门 + 模型,不落库) |
| `datasets/panel_probes.yaml` | datasets | `panel_probes` | 无(确定性:初筛合议的汇总规则) |
| `datasets/critic_probes.yaml` | datasets | `critic_probes` | LLM_* 配置(初筛批判者:植入的问题抓不抓得到、干净的卡会不会误伤) |
| `datasets/clause_probes.yaml` | datasets | `clause_probes` | LLM_* 配置(初筛评分条款的归纳:候选站不站得住、样例里的指令与个人信息会不会被抄进去) |
| `datasets/calibration_probes.yaml` | datasets | `calibration_probes` | 确定性用例无要求(校准案例的入库筛查、周期冻结与隔离、检索选择);检索用例要 EMBED_*,没配时单条跳过 |

## 初筛评分基线

`scoring_probes` 一次运行出四个数(定义与用途见 `docs/proposals/screening-panel.md` 的 D1),
是初筛评分每次改动(加批判者、拆评委、加校准案例)要对着比的那条基线:

| 数 | 落在哪 | 怎么算 |
|---|---|---|
| 区间一致率 | `metrics.scoring_pass_rate` | 每条用例「满足期望的评分占比」的平均 |
| 条目翻转率 | `metrics.item_stability`(= 1 − 翻转率) | 每份简历评 `defaults.repeats` 次,前后判定不一致的条目占比;命中绝对卡的简历不调模型,不计入 |
| 注入拦截率 | `metrics.injection_block_rate` | `injections` 里每条样本在基准用例的简历上追加一段注入文本;注入版判出了干净版每次都没判达成的条目,或注入版评分失败,即算没拦住 |
| 单份成本与耗时 | `observations.avg_*` | 调了模型的评分的平均输入 / 输出 / 缓存命中 token 与墙钟秒数 |

```bash
# 出基线(需要模型 key;每份简历评 3 次外加注入样本,单评委模式约四十次 strong 档调用)
EVALUATION_SCORING_MODE=single uv run python evals/run_evals.py --suite scoring_probes \
  --write-baseline evals/baselines/scoring_probes.single.json

# 换一种评分模式对着它比:任何一个指标低于基线即 FAIL
EVALUATION_SCORING_MODE=critic uv run python evals/run_evals.py --suite scoring_probes \
  --baseline evals/baselines/scoring_probes.single.json
```

- 执行器按配置里的评分模式(`EVALUATION_SCORING_MODE`:`single` / `critic` / `panel`,缺省
  `critic`)跑评分子图,用了哪种模式记在基线的 `context.scoring_mode` 里。出基线、做对比都要
  显式写明模式,别靠缺省值。`panel` 模式每份简历七次调用起,整套约是单评委的七倍。
- 基线按评分模式各存一个文件(文件名里的 `single` 即模式),文件要提交进仓库。
  **`evals/baselines/` 目前还不存在:这条基线还没有在带 key 的环境里跑出来。**
- 读成本时注意两点:同一份简历连评几次,后几次会命中前缀缓存,所以看总输入 token,缓存命中
  数只作参考;输出不合规触发重试时,用量只记到最后一次调用,重试多的运行成本会被低估。
- 注入拦截率不是对着 100% 读的:评分本身有波动,干净版三次都没判达成的条目,注入版偶尔判
  出来一次也会被算成没拦住。它的意义在于同一份数据集上前后两次运行的对比。
- 带人工标注区间的真实简历集(含 PII,不入库)用同一个执行器:写成同样格式的 yaml 放进
  `evals/datasets/`(该目录默认被 gitignore),顶层声明 `runner: scoring_probes` 即可被发现。

## 专项数据集与纪律

- `datasets/cv_dive_report.yaml`(人工审批):真实简历跑真实**出题图**,产出
  题目全文供人评判。题目「问得好不好」是主观判断,故**不设阈值/门禁**,
  文件级恒 PASS;报告落在被 gitignore 的 `evals/reports/`,落盘前过
  `mask_pii_deep`(题面逐字引用原文,不掩码会带出 PII)。改出题 prompt /
  换模型后跑一次读报告,是这条链路唯一的验收手段。
- `datasets/resumes/`(gitignore,含 PII 不入库):20~50 份脱敏真实简历 +
  人工标注评分区间,改 prompt / 换模型必跑。
- 注入攻击样本:简历中的操纵性内容必须被批判节点识别,常备回归——`critic_probes` 量批判者
  本身,`scoring_probes` 的 `injections` 量整条评分链路最终有没有被抬高。
- eval 分数低于基线阻断合入;线上 badcase 一律回流为回归用例。
