"""B 简历初筛评分子图(spec #135;规则决策 #123)。

- scoring.py:绝对卡确定性短路(进模型前),纯函数零 IO
- schema.py:仓库首个 strict Pydantic 结构化输出契约
- graph.py:langgraph 评分子图(precheck → llm_score → finalize)
- prompts/evaluation/scoring.md:打分 prompt(ADR-0004:唯一权威是文件)

数据面:state/evaluation.py(evaluation_scorecard,版本递增旧版保留)。
触发与队列:B2(asyncio task runner + 0 分队列标记),本票只交付子图。
"""

from official_agent.evaluation.schema import (
    AttitudeVerdict,
    DimensionScore,
    ScorecardOutput,
)

__all__ = ["AttitudeVerdict", "DimensionScore", "ScorecardOutput"]
