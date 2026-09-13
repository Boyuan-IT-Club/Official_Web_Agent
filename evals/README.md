# Evals

agent 的"单测"。统一 runner(#148,OBS-03 收口)执行,分两层 + 专项数据集:

1. **用例断言**(`evals/cases/*.yaml`):给定输入,断言 agent 行为(调了哪些
   工具/参数)——真实 LLM 驱动真实 ReAct 图,后端 HTTP 由 canned fake 顶替。
2. **阈值型门禁**(`evals/datasets/*.yaml`):数据集指标 ≥ 基线,负例不误命中
   ——真实 embedding + 真实检索链,不 mock。
3. **终答质量**(LLM-as-judge,#155 预留):正确性 / 有用性 / 语气。

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
  对比,任何指标低于基线 → REGRESSION → FAIL(OBS-07)。语料/embedding 模型
  变更后需重新校准并重写基线。

## Suite 一览

| Suite | 面 | kind | 环境要求 |
|---|---|---|---|
| `cases/tool_selection.yaml` | cases | `tool_selection` | LLM_* 配置 |
| `datasets/kb_probes.yaml` | datasets | `kb_probes` | kb.store 可导入 + EMBED_* + 语料入库 |

## 专项数据集与纪律

- `datasets/resumes/`(gitignore,含 PII 不入库):20~50 份脱敏真实简历 +
  人工标注评分区间,改 prompt / 换模型必跑(OBS-04,#158)。
- 注入攻击样本:简历中的操纵性内容必须被批判节点识别(SEC-04),常备回归。
- eval 分数低于基线阻断合入(OBS-07);线上 badcase 一律回流为回归用例(OBS-06)。
