"""简历初筛评分子图。

- scoring.py:绝对卡确定性短路(进模型前)+ 清单派生分数,纯函数零 IO
- grading.py:同部门候选池内的相对分级(优秀/良好/一般)与调剂/面试提示
- applicant.py:报名信息(志愿部门/专业/年级)的解析
- schema.py:仓库首个 strict Pydantic 结构化输出契约
- graph.py:langgraph 评分子图(precheck → llm_score → finalize)
- prompts/evaluation/scoring.md:打分 prompt(ADR-0004:唯一权威是文件)

数据面:state/evaluation.py(evaluation_scorecard,版本递增旧版保留)。
触发与队列:进程内 asyncio task runner + 0 分队列标记(见 runner.py)。
"""

from official_agent.evaluation.schema import (
    DEPARTMENTS,
    DEPT_MATCH_ITEMS,
    EFFORT_ITEMS,
    AttitudeVerdict,
    DeptMatchVerdict,
    ItemVerdict,
    ScorecardOutput,
)

__all__ = [
    "DEPARTMENTS",
    "DEPT_MATCH_ITEMS",
    "EFFORT_ITEMS",
    "AttitudeVerdict",
    "DeptMatchVerdict",
    "ItemVerdict",
    "ScorecardOutput",
]
