"""初筛分级:同部门候选池里相对划分「优秀 / 良好 / 一般」,纯函数零 IO。

分数(0-10)是清单派生的**绝对值**,只用来排序;等级是**相对的**——拿同一周期、
同一第一志愿部门的候选人放在一起比,头部约两成优秀、尾部约两成一般、中间良好,
形状接近正态分布的「两头少中间多」。这样各部门的等级分布一致,不会因为某个
部门标准松、另一个部门标准严,就一边全是优秀、一边全是一般。

相对划分有两个失真场景,用绝对锚点兜住:
- **池子太小**(不足 min_pool 人):比例没有统计意义(5 个人里的「前两成」
  就是 1 个人),改按绝对分数划档;
- **池子整体偏高或偏低**:全员都达标时,排在后两成的人也不该被叫做「一般」;
  全员都没写什么时,排在前两成的人也不该被叫做「优秀」——分别由
  good_floor 与 excellent_floor 两条保底线处理。

同分一律取**对候选人有利**的一档:卡在比例边界上的同分者要么全进上一档,
要么全留在上一档,不会出现同分不同级。

等级在**读取时**计算:新简历进池、旧简历重评都会让边界移动,存死的等级会过时。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from official_agent.evaluation.applicant import ApplicantProfile, is_cs_major, is_freshman
from official_agent.evaluation.schema import DEPARTMENTS

Grade = Literal["优秀", "良好", "一般"]

#: 候选池的键:第一志愿部门;没填志愿的单独成池。
NO_DEPT_POOL = "未填志愿"


@dataclass(frozen=True)
class GradingPolicy:
    """分级参数。缺省值即招新约定;部署侧经 Settings 覆盖。"""

    top_ratio: float = 0.2  # 头部多少比例为优秀
    bottom_ratio: float = 0.2  # 尾部多少比例为一般
    min_pool: int = 15  # 池子不足这么多人时改用绝对锚点
    anchor_excellent: float = 9.0  # 绝对锚点:达到即优秀(小池子用)
    anchor_good: float = 7.0  # 绝对锚点:达到即良好(小池子用);也是大池子里「至少良好」的保底线
    excellent_floor: float = 5.0  # 分数低于此,排名再靠前也不给优秀


@dataclass(frozen=True)
class GradeResult:
    """一个维度的分级结果。basis:pool=池内相对划分,anchor=绝对锚点。"""

    level: Grade | None
    basis: Literal["pool", "anchor", "hard_zero"] | None = None
    pool_size: int = 0


def grade_score(score: float | None, pool: list[float], policy: GradingPolicy) -> GradeResult:
    """单个分数在其候选池里的等级。pool 应包含该候选人自己的分数。"""
    if score is None:
        return GradeResult(level=None)
    n = len(pool)
    if n < policy.min_pool:
        if score >= policy.anchor_excellent:
            level: Grade = "优秀"
        elif score >= policy.anchor_good:
            level = "良好"
        else:
            level = "一般"
        return GradeResult(level=level, basis="anchor", pool_size=n)

    above = sum(1 for s in pool if s > score)
    at_or_below = n - above
    if above / n < policy.top_ratio:
        level = "优秀"
    elif at_or_below / n <= policy.bottom_ratio:
        level = "一般"
    else:
        level = "良好"
    # 保底线:池子整体偏高时,达到良好锚点的人不叫「一般」;整体偏低时,
    # 连一半都没达成的人不叫「优秀」;一项都没达成的人无论排名都是「一般」
    if level == "一般" and score >= policy.anchor_good:
        level = "良好"
    if level == "优秀" and score < policy.excellent_floor:
        level = "良好"
    if score <= 0:
        level = "一般"
    return GradeResult(level=level, basis="pool", pool_size=n)


@dataclass(frozen=True)
class PoolEntry:
    """参与分级的一张卡(每份简历取最新版)。"""

    resume_id: int
    first_dept: str | None
    match_score: float | None
    effort_score: float | None
    hard_zero: bool = False


@dataclass(frozen=True)
class CardGrades:
    """一份简历两个维度的等级。"""

    match: GradeResult = field(default_factory=lambda: GradeResult(level=None))
    effort: GradeResult = field(default_factory=lambda: GradeResult(level=None))
    pool: str = NO_DEPT_POOL


def pool_key(first_dept: str | None) -> str:
    """第一志愿 → 候选池名。媒体部与综合部各自成池。"""
    return first_dept if first_dept in DEPARTMENTS else NO_DEPT_POOL


def grade_pool(entries: list[PoolEntry], policy: GradingPolicy) -> dict[int, CardGrades]:
    """整个周期的卡 → 每份简历的两维等级。按第一志愿分池,两个维度各自在池内排。

    命中绝对卡(hard_zero)的简历两维都是「一般」,但仍留在池里参与排名——
    它们本来就在尾部,拿掉反而会把正常简历挤进尾部两成。
    """
    pools: dict[str, list[PoolEntry]] = {}
    for entry in entries:
        pools.setdefault(pool_key(entry.first_dept), []).append(entry)

    result: dict[int, CardGrades] = {}
    for name, members in pools.items():
        match_pool = [e.match_score for e in members if e.match_score is not None]
        effort_pool = [e.effort_score for e in members if e.effort_score is not None]
        for entry in members:
            if entry.hard_zero:
                zero = GradeResult(level="一般", basis="hard_zero", pool_size=len(members))
                result[entry.resume_id] = CardGrades(match=zero, effort=zero, pool=name)
                continue
            # 没填志愿就无从谈「与志愿部门的匹配度」,只给认真程度
            match = (
                GradeResult(level=None)
                if name == NO_DEPT_POOL
                else grade_score(entry.match_score, match_pool, policy)
            )
            effort = grade_score(entry.effort_score, effort_pool, policy)
            result[entry.resume_id] = CardGrades(match=match, effort=effort, pool=name)
    return result


# ── 调剂建议与面试提示:评分时算好随卡落库(只依赖这一份简历,不随池子变) ──

#: 调剂建议的门槛:别的部门至少比志愿部门高这么多分,且本身达到这个分数。
#: 两条都要满足——只高一点点不值得打扰,绝对分太低的「更匹配」也没有意义。
TRANSFER_MARGIN = 3.0
TRANSFER_FLOOR = 6.0


def transfer_hint(
    match_scores: dict[str, float], profile: ApplicantProfile
) -> dict[str, object] | None:
    """简历明显更贴合别的部门时给出调剂建议;否则 None。

    对应招新约定:简历优秀但意向部门与内容不符,要勾选调剂、选择调剂部门。
    同分时优先第二志愿,其次按部门顺序。
    """
    first, second = profile.first_dept, profile.second_dept
    base = match_scores.get(first, 0.0) if first else 0.0
    others = [d for d in DEPARTMENTS if d != first and d in match_scores]
    if not others:
        return None
    best = max(others, key=lambda d: (match_scores[d], d == second, -DEPARTMENTS.index(d)))
    best_score = match_scores[best]
    if best_score < TRANSFER_FLOOR or best_score - base < TRANSFER_MARGIN:
        return None
    return {"dept": best, "is_second_choice": best == second}


def interview_hints(
    profile: ApplicantProfile,
    *,
    match_met: dict[str, dict[str, bool]],
    transfer: dict[str, object] | None,
) -> list[str]:
    """给面试官的提示。只提示「面试里要问什么」,不影响分数与等级。"""
    hints: list[str] = []
    first = profile.first_dept
    if first is None:
        hints.append("未填志愿部门:面试时先确认意愿部门")
    if is_cs_major(profile.major) is False:
        hints.append(
            f"非计算机类专业({profile.major}):请询问未来规划与对计算机的了解程度并记录;"
            "若未来不打算从事计算机相关方向,可不做考虑"
        )
    if first == "技术部":
        if not match_met.get("技术部", {}).get("技术基础", False):
            hints.append(
                "志愿技术部但简历未体现技术基础:技术部投递扎堆,若面试中也看不到强烈的"
                "加入热情或其他突出能力(活动组织、媒体技能等),不建议录入技术部"
            )
        if is_freshman(profile.grade) is False:
            hints.append("非新生报技术部:面试需对技术提出较高要求")
    if transfer:
        dept = transfer["dept"]
        suffix = "(即第二志愿)" if transfer.get("is_second_choice") else ""
        hints.append(f"简历内容更贴合{dept}{suffix}:可询问调剂意愿")
    return hints
