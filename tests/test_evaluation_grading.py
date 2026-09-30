"""初筛分级与报名信息解析的纯逻辑单测。"""

from official_agent.evaluation.applicant import (
    ApplicantProfile,
    is_cs_major,
    is_freshman,
    normalize_dept,
    parse_intended_departments,
)
from official_agent.evaluation.grading import (
    NO_DEPT_POOL,
    GradingPolicy,
    PoolEntry,
    grade_pool,
    grade_score,
    interview_hints,
    transfer_hint,
)

_POLICY = GradingPolicy()


# ── 报名信息解析 ───────────────────────────────────────


def test_parse_intended_departments_json_array() -> None:
    """后端 expected_departments 存 JSON 数组;「无」不是部门。"""
    assert parse_intended_departments('["技术部","项目部"]') == ("技术部", "项目部")
    assert parse_intended_departments('["媒体部","无"]') == ("媒体部", None)
    assert parse_intended_departments('["综合部","综合部"]') == ("综合部", None)


def test_parse_intended_departments_legacy_formats() -> None:
    """老数据:逗号分隔(含全角)、单个部门名、空值。"""
    assert parse_intended_departments("技术部,媒体部") == ("技术部", "媒体部")
    assert parse_intended_departments("项目部,综合部") == ("项目部", "综合部")
    assert parse_intended_departments("媒体部") == ("媒体部", None)
    assert parse_intended_departments("") == (None, None)
    assert parse_intended_departments('"技术部"') == ("技术部", None)


def test_normalize_dept_prefers_full_name() -> None:
    """先认全名再认简称:「项目部(偏技术)」不能被「技术」截走。"""
    assert normalize_dept("项目部(偏技术)") == "项目部"
    assert normalize_dept("技术") == "技术部"
    assert normalize_dept("宣传部") is None
    assert normalize_dept("无") is None


def test_cs_major_and_freshman() -> None:
    assert is_cs_major("软件工程") is True
    assert is_cs_major("计算机科学与技术") is True
    assert is_cs_major("数据科学与大数据技术") is True
    assert is_cs_major("汉语言文学") is False
    assert is_cs_major("") is None  # 没填 ≠ 非计算机类
    assert is_freshman("大一") is True
    assert is_freshman("大三") is False
    assert is_freshman("") is None


# ── 池内相对分级 ───────────────────────────────────────


def test_small_pool_uses_absolute_anchors() -> None:
    """池子不足 min_pool:比例没有统计意义,按绝对锚点划档。"""
    pool = [9.0, 7.0, 3.0]
    assert grade_score(9.0, pool, _POLICY).level == "优秀"
    assert grade_score(7.0, pool, _POLICY).level == "良好"
    assert grade_score(3.0, pool, _POLICY).level == "一般"
    assert grade_score(9.0, pool, _POLICY).basis == "anchor"


def test_large_pool_splits_twenty_sixty_twenty() -> None:
    """20 人各不同分:前 4 优秀、后 4 一般、中间良好(两头少中间多)。"""
    pool = [i * 0.5 for i in range(20)]  # 0.0 … 9.5
    levels = [grade_score(s, pool, _POLICY).level for s in sorted(pool, reverse=True)]
    assert levels.count("优秀") == 4
    assert levels.count("一般") == 4
    assert levels.count("良好") == 12
    assert grade_score(9.5, pool, _POLICY).basis == "pool"


def test_ties_resolve_toward_better_grade() -> None:
    """边界上的同分者同级,且取对候选人有利的一档。"""
    pool = [8.0] * 6 + [5.0] * 14  # 6/20=30% 同为 8 分,超过头部两成
    assert grade_score(8.0, pool, _POLICY).level == "优秀"  # 同分全进优秀
    pool = [6.0] * 10 + [4.0] * 6 + [4.5] * 4
    # 4.0 占 30%:按「有利」规则不全落一般(≤ 两成才算尾部)
    assert grade_score(4.0, pool, _POLICY).level == "良好"


def test_floors_guard_against_skewed_pools() -> None:
    """整体偏高:达到良好锚点不叫一般;整体偏低:不到一半不叫优秀;0 分恒一般。"""
    high = [10.0] * 17 + [7.0] * 3  # 7 分排在尾部 15%
    assert grade_score(7.0, high, _POLICY).level == "良好"
    low = [4.0] * 3 + [1.0] * 17
    assert grade_score(4.0, low, _POLICY).level == "良好"  # 排名靠前但不到优秀保底线
    zeros = [0.0] * 20
    assert grade_score(0.0, zeros, _POLICY).level == "一般"


def test_grade_pool_separates_departments() -> None:
    """按第一志愿分池:媒体部与综合部各自成池,互不影响。"""
    entries = [
        PoolEntry(resume_id=i, first_dept="技术部", match_score=float(i % 10), effort_score=6.0)
        for i in range(20)
    ]
    entries += [
        PoolEntry(resume_id=100, first_dept="媒体部", match_score=9.0, effort_score=9.0),
        PoolEntry(resume_id=101, first_dept="综合部", match_score=3.0, effort_score=9.0),
        PoolEntry(resume_id=102, first_dept=None, match_score=None, effort_score=7.0),
    ]
    grades = grade_pool(entries, _POLICY)
    assert grades[100].pool == "媒体部" and grades[101].pool == "综合部"
    assert grades[100].match.basis == "anchor"  # 小池子
    assert grades[100].match.level == "优秀"
    assert grades[101].match.level == "一般"
    assert grades[0].match.basis == "pool" and grades[0].match.pool_size == 20
    # 没填志愿:只有认真程度等级
    assert grades[102].pool == NO_DEPT_POOL
    assert grades[102].match.level is None
    assert grades[102].effort.level == "良好"


def test_hard_zero_is_always_general() -> None:
    entries = [PoolEntry(resume_id=1, first_dept="技术部", match_score=9.0, effort_score=9.0,
                         hard_zero=True)]
    grades = grade_pool(entries, _POLICY)
    assert grades[1].match.level == grades[1].effort.level == "一般"
    assert grades[1].effort.basis == "hard_zero"


# ── 调剂建议与面试提示 ─────────────────────────────────


def test_transfer_hint_requires_margin_and_floor() -> None:
    profile = ApplicantProfile(first_dept="技术部", second_dept="媒体部")
    scores = {"技术部": 2.0, "项目部": 4.0, "媒体部": 8.0, "综合部": 8.0}
    # 同分时优先第二志愿
    assert transfer_hint(scores, profile) == {"dept": "媒体部", "is_second_choice": True}
    # 只高一点:不打扰
    assert transfer_hint({**scores, "技术部": 6.0}, profile) is None
    # 绝对分太低的「更匹配」没有意义
    assert transfer_hint({"技术部": 0.0, "项目部": 5.0, "媒体部": 5.0, "综合部": 0.0},
                         profile) is None


def test_interview_hints_follow_recruiting_notes() -> None:
    profile = ApplicantProfile(first_dept="技术部", major="汉语言文学", grade="大二")
    hints = interview_hints(profile, match_met={"技术部": {"技术基础": False}}, transfer=None)
    text = " ".join(hints)
    assert "非计算机类专业(汉语言文学)" in text
    assert "未体现技术基础" in text
    assert "非新生报技术部" in text
    # 计算机类大一且有技术基础:没有提示
    ok = ApplicantProfile(first_dept="技术部", major="软件工程", grade="大一")
    assert interview_hints(ok, match_met={"技术部": {"技术基础": True}}, transfer=None) == []
