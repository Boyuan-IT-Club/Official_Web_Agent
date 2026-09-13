"""B1 绝对卡确定性短路纯逻辑单测(#123:进模型前的态度不端硬判)。"""

from official_agent.evaluation.scoring import (
    FieldText,
    detect_hard_zero,
    is_hard_zero_value,
    weighted_total,
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


def test_weighted_total_uses_configured_weights() -> None:
    assert weighted_total({"a": 80, "b": 40}, {"a": 3, "b": 1}) == 70.0
    assert weighted_total({"a": 80}, {}) == 80.0  # 缺省权重 1
    assert weighted_total({}, {}) == 0.0


def test_exact_placeholder_match_is_hard_zero() -> None:
    """B2 接线后有真实 placeholder:全等判,比前缀启发式更准。"""
    ph = "介绍一下你参与过的项目、承担的角色和最终成果"
    assert is_hard_zero_value(ph, placeholder=ph)
    # 真认真写了(比 placeholder 长且不同)→ 不卡
    assert not is_hard_zero_value("我做过社团官网重构,负责报名页。", placeholder=ph)
