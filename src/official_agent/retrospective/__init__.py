"""招新周期复盘:每个周期留一份纯代码算出的指标快照,作跨周期对比的基线。

设计见 docs/proposals/cycle-retrospective.md。本包不含任何模型调用;
对后端只读,自己的状态只落 agent 的 Postgres(state/retrospective)。
"""
