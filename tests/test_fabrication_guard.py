"""编造守卫测试(#161,GRA-04):话术族拦截/诚实话术放行/整段改写。"""

from official_agent.security.fabrication_guard import (
    GUARD_NAME,
    guard_empty_tools_reply,
)


def test_fabricated_claim_gets_rewritten() -> None:
    """GRA-04 生产复现原案:tools=[] 却回复「查询结果:…」。"""
    fabricated = "查询结果:您有 3 份简历待筛选。"
    final, verdict = guard_empty_tools_reply(fabricated)
    assert verdict.startswith("triggered:")
    assert "查询" not in final or "无法查询" in final
    assert "没有可用的数据查询权限" in final


def test_all_claim_wordings_trigger() -> None:
    for text in (
        "我查了系统,目前有 2 个开放周期。",
        "已查询到您的面试安排。",
        "检索到以下简历:",
        "根据查询,统计如下:",
    ):
        _, verdict = guard_empty_tools_reply(text)
        assert verdict.startswith("triggered:"), text


def test_honest_replies_pass_through() -> None:
    honest = "我当前没有查询权限,建议你到管理端查看简历列表。"
    final, verdict = guard_empty_tools_reply(honest)
    assert verdict == "clean"
    assert final is honest or final == honest


def test_empty_and_plain_text() -> None:
    assert guard_empty_tools_reply("") == ("", "clean")
    final, verdict = guard_empty_tools_reply("你好,我是博远社团的助手。")
    assert verdict == "clean" and final == "你好,我是博远社团的助手。"


def test_guard_name_constant() -> None:
    """守卫轻契约(#159):guard_name 稳定,trace 字段在 #163 统一接线。"""
    assert GUARD_NAME == "fabrication_empty_tools"


def test_reddit_escape_variants_trigger() -> None:
    """评审 P1 实锤的两组同族逃逸:查到了(无「如下」)/已经查询(非已查询)。"""
    for text in ("为你查到了 3 份简历。", "我已经查询过了,数据如下:"):
        _, verdict = guard_empty_tools_reply(text)
        assert verdict.startswith("triggered:"), text


def test_negated_claims_are_honest_and_pass() -> None:
    """否定前缀白名单:「没有查询结果」类诚实话术不触发。"""
    for text in (
        "很抱歉,没有查询结果。",
        "我无法查询到相关数据,请到管理端查看。",
        "我未能查到任何记录,建议联系管理员。",
    ):
        final, verdict = guard_empty_tools_reply(text)
        assert verdict == "clean", text
        assert final == text


def test_soothing_prefix_with_claim_triggers() -> None:
    """「别」是安抚词不是否定:别担心+编造结论必须触发(复审 P1)。"""
    for text in (
        "别担心,查询到您有 3 份简历待筛选。",
        "别急,已查询到您的简历已进入下一轮。",
    ):
        _, verdict = guard_empty_tools_reply(text)
        assert verdict.startswith("triggered:"), text
