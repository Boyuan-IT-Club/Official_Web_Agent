"""绝对卡确定性短路纯逻辑单测(进模型前的态度不端硬判)。"""

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
    """接线后有真实 placeholder:全等判,比前缀启发式更准。"""
    ph = "介绍一下你参与过的项目、承担的角色和最终成果"
    assert is_hard_zero_value(ph, placeholder=ph)
    # 真认真写了(比 placeholder 长且不同)→ 不卡
    assert not is_hard_zero_value("我做过社团官网重构,负责报名页。", placeholder=ph)


# ── 特质清单分段(总分由达成项数派生)────────────────────


def test_trait_score_bands_by_achievement_count() -> None:
    """达成项数决定分数段;段内按加权达成比例微调。

    分数只由判定结果派生:模型不给分,同一份判定重算必然同分。
    """
    from official_agent.evaluation.schema import TRAITS
    from official_agent.evaluation.scoring import trait_score

    def flags(k: int) -> dict[str, bool]:
        return {name: i < k for i, name in enumerate(TRAITS)}

    assert trait_score(flags(0)) == 0.0
    # 全达成落顶段
    assert trait_score(flags(len(TRAITS))) == 100.0
    # 每段的下限:达成数跨过阈值就进该段
    assert 90 <= trait_score(flags(11)) <= 100
    assert 75 <= trait_score(flags(9)) < 90
    assert 60 <= trait_score(flags(7)) < 75
    assert 45 <= trait_score(flags(5)) < 60
    assert 30 <= trait_score(flags(3)) < 45
    assert 15 <= trait_score(flags(1)) < 30


def test_trait_score_is_order_independent() -> None:
    """判定集相同就同分,与模型输出顺序无关。

    回归:先前按**列表位置**加权,而校验只比对集合——模型打乱输出顺序会
    把权重加到错的项上,同一个判定集换个顺序就换了分数。
    """
    from official_agent.evaluation.schema import TRAITS
    from official_agent.evaluation.scoring import trait_score

    core = {name: i < 9 for i, name in enumerate(TRAITS)}
    shuffled = dict(reversed(list(core.items())))
    assert trait_score(core) == trait_score(shuffled)


def test_trait_score_weights_leading_traits_higher() -> None:
    """同样达成 9 项,达成**靠前**特质的得分高于只达成靠后的。

    靠前的特质是招新更看重的(经验/技术/自学/开源),同数量下应当更高。
    """
    from official_agent.evaluation.schema import TRAITS
    from official_agent.evaluation.scoring import trait_score

    lead = {name: i < 9 for i, name in enumerate(TRAITS)}
    tail = {name: i >= 3 for i, name in enumerate(TRAITS)}
    assert sum(lead.values()) == sum(tail.values()) == 9
    assert trait_score(lead) > trait_score(tail)


def test_trait_score_ignores_unknown_traits() -> None:
    """清单外的键不计入 —— 模型自造项名不能虚增达成数。"""
    from official_agent.evaluation.schema import TRAITS
    from official_agent.evaluation.scoring import trait_score

    assert trait_score({**{n: False for n in TRAITS}, "自造特质": True}) == 0.0


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
