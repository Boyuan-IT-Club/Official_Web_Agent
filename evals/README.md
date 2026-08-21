# Evals

agent 的"单测"。分三层(OBS-03/04/05):

1. **工具选择正确性**(确定性断言,进 CI):给定输入,agent 该调哪个工具 —— `cases/tool_selection.yaml`
2. **参数正确性**(确定性断言,进 CI):调用参数是否符合期望
3. **终答质量**(LLM-as-judge):正确性 / 有用性 / 语气

专项数据集:

- `datasets/resumes/`(gitignore,含 PII 不入库):20~50 份脱敏真实简历 + 人工标注评分区间,改 prompt / 换模型必跑(OBS-04)
- 注入攻击样本:简历中的操纵性内容必须被批判节点识别(SEC-04),常备回归

约定:eval 分数低于基线阻断合入(OBS-07);线上 badcase 一律回流为回归用例(OBS-06)。
