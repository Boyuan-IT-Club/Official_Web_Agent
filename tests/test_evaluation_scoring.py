"""绝对卡确定性短路纯逻辑单测(进模型前的态度不端硬判)。"""

from official_agent.evaluation.schema import DEPT_MATCH_ITEMS, EFFORT_ITEMS
from official_agent.evaluation.scoring import (
    FieldText,
    checklist_score,
    detect_hard_zero,
    effort_ceiling,
    is_hard_zero_value,
)


def test_blank_and_single_char_are_hard_zero() -> None:
    assert is_hard_zero_value("")
    assert is_hard_zero_value("   ")
    assert is_hard_zero_value("无")
    assert is_hard_zero_value("a")


def test_repeated_char_and_digits_are_hard_zero() -> None:
    assert is_hard_zero_value("111")
    assert is_hard_zero_value("。。。")
    assert is_hard_zero_value("2024.9")
    assert is_hard_zero_value("1,1,1")


def test_placeholder_and_self_copy_are_hard_zero() -> None:
    assert is_hard_zero_value("请输入你的自我介绍")
    assert is_hard_zero_value("略")
    assert is_hard_zero_value("同上")
    assert is_hard_zero_value("自我介绍", title="自我介绍")  # 抄字段名


def test_normal_answers_pass() -> None:
    assert not is_hard_zero_value("我来自计算机专业,做过两个 Web 项目,熟悉 Django。")
    assert not is_hard_zero_value("在大二时加入了校科协,负责招新宣讲。")
    assert not is_hard_zero_value("有 10 个成员的团队负责人。")  # 含数字但非纯数字
    assert is_hard_zero_value("10")  # 纯数字主观作答仍是硬 0


def test_detect_hard_zero_collects_only_offenders() -> None:
    fields = [
        FieldText(field_key="intro", title="自我介绍", value="我是张三,热爱编程。"),
        FieldText(field_key="reason", title="加入理由", value="111"),
        FieldText(field_key="projects", title="项目经验", value="  "),
    ]
    reasons = detect_hard_zero(fields)
    assert set(reasons) == {"reason", "projects"}
    assert "单字符重复" in reasons["reason"]
    assert "空白" in reasons["projects"]


def test_detect_hard_zero_empty_when_all_normal() -> None:
    fields = [FieldText(field_key="intro", title="自我介绍", value="认真的回答。")]
    assert detect_hard_zero(fields) == {}


def test_exact_placeholder_match_is_hard_zero() -> None:
    """接线后有真实 placeholder:全等判,比前缀启发式更准。"""
    ph = "介绍一下你参与过的项目、承担的角色和最终成果"
    assert is_hard_zero_value(ph, placeholder=ph)
    # 真认真写了(比 placeholder 长且不同)→ 不卡
    assert not is_hard_zero_value("我做过社团官网重构,负责报名页。", placeholder=ph)


# ── 清单派生分数 ───────────────────────────────────


def test_checklist_score_is_sum_of_met_weights() -> None:
    """分数 = 达成项权重之和(清单权重和为 10)。模型不给分,同一份判定重算必然同分。"""
    tech = DEPT_MATCH_ITEMS["技术部"]
    assert checklist_score({}, tech) == 0.0
    assert checklist_score({n: True for n, _ in tech}, tech) == 10.0
    assert checklist_score({"技术基础": True}, tech) == 4.0  # 技术能力优先:单项占四成
    assert checklist_score({"技术基础": True, "探索内驱力": True}, tech) == 7.0


def test_checklist_score_is_order_independent() -> None:
    """判定集相同就同分,与模型输出顺序无关(按项名取值,不按位置)。"""
    met = {n: i % 2 == 0 for i, (n, _) in enumerate(EFFORT_ITEMS)}
    shuffled = dict(reversed(list(met.items())))
    assert checklist_score(met, EFFORT_ITEMS) == checklist_score(shuffled, EFFORT_ITEMS)


def test_checklist_score_ignores_unknown_items() -> None:
    """清单外的键不计入 —— 模型自造项名不能虚增分数。"""
    assert checklist_score({"自造判定项": True}, EFFORT_ITEMS) == 0.0


def test_effort_ceiling_bands() -> None:
    """篇幅封顶:一段话 3、两三行 6、够长不封顶;空简历也封顶(兜底)。"""

    def one(n: int) -> list[FieldText]:
        return [FieldText(field_key="intro", title="自我介绍", value="字" * n)]

    assert effort_ceiling(one(80)) == 3.0
    assert effort_ceiling(one(200)) == 6.0
    assert effort_ceiling(one(400)) is None
    assert effort_ceiling([]) == 3.0


# ── 可选栏留空不算敷衍 ───────────────────────────────


def test_optional_blank_is_not_hard_zero() -> None:
    """可选栏没填是正常的 —— 不能据此把整份判成敷衍。

    「获奖经历」这类字段 is_required=0,候选人没有奖项就空着。旧实现把
    任何空值都当绝对卡,一份其他栏都认真的简历会因此整份硬 0(初筛不过)。
    """
    assert is_hard_zero_value("", required=False) is False
    assert is_hard_zero_value("   ", required=False) is False
    # 必填留空仍然卡
    assert is_hard_zero_value("", required=True) is True
    assert is_hard_zero_value("") is True  # 缺省保守


def test_optional_none_word_is_not_hard_zero() -> None:
    """可选栏写「无」= 留空,同样豁免。

    「没有获奖经历」在表单上有两种同义写法:空着,或者写「无」。豁免只做了
    留空那一半时,诚实写「无」的候选人整份硬 0(attitude=bad_faith),
    什么都不写的人反而照常进模型——同一件事两种判法。
    """
    for value in ("无", "无。", "暂无", "没有", "不涉及", "/", "-"):
        assert is_hard_zero_value(value, title="获奖经历", required=False) is False
    # 必填栏写「无」仍是敷衍:那一栏本来就该有内容
    assert is_hard_zero_value("无", title="项目经验", required=True) is True


def test_optional_blank_still_catches_filler() -> None:
    """可选**不代表免检**:填了敷衍内容一样卡(「没有这一项」和乱填是两回事)。"""
    assert is_hard_zero_value("a", required=False) is True
    assert is_hard_zero_value("111", required=False) is True
    assert is_hard_zero_value("同上", required=False) is True
    assert is_hard_zero_value("请填写获奖经历", required=False) is True


def test_detect_hard_zero_ignores_optional_blanks() -> None:
    """端到端:只有可选栏空着时,整份不判硬 0。"""
    fields = [
        FieldText(field_key="profile", title="个人简介", value="我是张三,做过两个 Web 项目。"),
        FieldText(field_key="awards", title="获奖经历", value="", required=False),
        FieldText(field_key="internship", title="实习经历", value="无", required=False),
    ]
    assert detect_hard_zero(fields) == {}
    # 必填栏空着仍要卡
    fields.append(FieldText(field_key="projects", title="项目经验", value="", required=True))
    assert set(detect_hard_zero(fields)) == {"projects"}
