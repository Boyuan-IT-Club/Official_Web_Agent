"""技术栈抽取测试:定位/证据闸门/档位/去重截断/编排短路。

测试文本取自真实简历的**结构**(技术栈栏无分隔符连写、项目栏紧随其后),
不引用真实简历文件——真实简历是 gitignore 的评测样本,不进单测。
"""

import json
from typing import Any

import pytest

from official_agent.evaluation import tech_stack as ts

# ── 板块定位(纯函数) ──────────────────────────────


def test_locate_tech_section_stops_at_next_board() -> None:
    text = "技术栈:\nPython、PyTorch\n项目经验:\n钢材缺陷检测\n自我介绍:\n爱笑"
    assert ts.locate_tech_section(text) == "Python、PyTorch"


def test_locate_tech_section_takes_inline_body() -> None:
    """「技术栈:Python」单行写法——丢掉同行正文等于丢掉整个技术栈栏。"""
    text = "技术栈:Python、Java\n自我介绍:\n认真"
    assert ts.locate_tech_section(text) == "Python、Java"


def test_locate_sections_absent_returns_empty() -> None:
    assert ts.locate_tech_section("自我介绍:\n爱笑") == ""
    assert ts.locate_project_section("自我介绍:\n爱笑") == ""


def test_locate_project_section_bounded_by_self_intro() -> None:
    text = "项目经验:\n停车管理系统\n自我介绍:\n爱笑\n加入理由:\n想学习"
    assert ts.locate_project_section(text) == "停车管理系统"


def test_header_without_colon_is_recognized() -> None:
    """真实简历里标题常无冒号(「技术栈」独占一行)。"""
    assert ts.locate_tech_section("技术栈\nPythonC\n项目经验\n做过网站") == "PythonC"


# ── 证据闸门(纯函数) ──────────────────────────────

_SOURCE = "技术栈:\nPython、PyTorch、ResNet\n项目经验:\n钢材缺陷检测用了 YOLO11n"


def test_verify_evidence_accepts_verbatim() -> None:
    assert ts.verify_evidence("PyTorch", "Python、PyTorch、ResNet", _SOURCE)


def test_verify_evidence_ignores_case_and_space() -> None:
    """中英混排空格不稳(VS Code / VSCode),逐字比对会误杀同一名词。"""
    assert ts.verify_evidence("vs code", "VSCode 用来写代码", "技术栈:VSCode 用来写代码")
    assert ts.verify_evidence("ResNet", "用 ResNet 做分类", "技术栈:用ResNet做分类")


def test_verify_evidence_rejects_fabricated_name() -> None:
    """简历没写 Kubernetes → 拒收,否则面试官会问到不存在的东西。"""
    assert not ts.verify_evidence("Kubernetes", "Python、PyTorch、ResNet", _SOURCE)


def test_verify_evidence_rejects_real_name_with_fake_quote() -> None:
    """真名词配假出处句同样拒收——否则 raw_text 失去「出处」含义。"""
    assert not ts.verify_evidence("PyTorch", "我精通 PyTorch 分布式训练", _SOURCE)


def test_verify_evidence_rejects_empty() -> None:
    assert not ts.verify_evidence("", "Python", _SOURCE)
    assert not ts.verify_evidence("Python", "", _SOURCE)
    assert not ts.verify_evidence("Python", "Python", "")


# ── 档位(纯函数) ──────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("精通 PyTorch", "精通"),
        ("熟练使用 PyTorch", "熟练"),
        ("掌握 Python 基础", "掌握"),
        ("了解 Transformer", "了解"),
        ("初步了解 Linux", "初学"),
        ("正在学习 Go", "初学"),
        ("用过 Docker", "初学"),
        ("", "listed"),
        ("Python", "listed"),
        ("说不清楚", "listed"),
    ],
)
def test_normalize_level(text: str, expected: str) -> None:
    assert ts.normalize_level(text) == expected


def test_normalize_level_negation_is_not_a_claim() -> None:
    """「不了解 Redis」不是把 Redis 列为技能。"""
    assert ts.normalize_level("不了解 Redis") == "listed"


# ── build_items:闸门 → 合并 → 截断(纯函数) ──────────


def test_build_items_rejects_fabricated_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    raw = [
        {"name": "Python", "raw_text": "Python、PyTorch、ResNet", "used_in": []},
        {"name": "Kubernetes", "raw_text": "精通 K8s 调度", "used_in": []},
    ]
    with caplog.at_level("WARNING"):
        items = ts.build_items(raw, _SOURCE)
    assert [i.name for i in items] == ["Python"]
    assert "tech_stack_evidence" in caplog.text


def test_normalize_level_shanchang_is_not_jingtong() -> None:
    """「最擅长 C/C++」= 熟练,不是精通(自我介绍栏常见措辞)。

    精通是最高档,抬上去会把面试官推向候选人答不上的深水区。
    """
    assert ts.normalize_level("我非常擅长 C/C++ 编程") == "熟练"
    assert ts.normalize_level("最擅长Python") == "熟练"
    assert ts.normalize_level("精通 C++") == "精通"


def test_build_items_dedupes_and_merges_project_usage() -> None:
    """同一技术出现在多个项目:并成一条,项目归属不丢。"""
    text = "技术栈:\nPython\n项目经验:\n钢材缺陷检测\n停车管理系统"
    raw = [
        {"name": "Python", "raw_text": "Python", "used_in": ["钢材缺陷检测"]},
        {"name": "python", "raw_text": "Python", "used_in": ["停车管理系统"]},
    ]
    items = ts.build_items(raw, text)
    assert len(items) == 1
    assert items[0].used_in == ("钢材缺陷检测", "停车管理系统")


def test_build_items_drops_fabricated_project_name() -> None:
    text = "技术栈:\nPython\n项目经验:\n停车管理系统"
    raw = [{"name": "Python", "raw_text": "Python", "used_in": ["某大厂实习", "停车管理系统"]}]
    items = ts.build_items(raw, text)
    assert items[0].used_in == ("停车管理系统",)


def test_build_items_truncates_to_limit_preserving_order() -> None:
    """简历可能列 9+ 个;取前 limit 个,顺序即醒目度。"""
    source = "技术栈:\n" + "\n".join(f"T{i}" for i in range(9))
    raw = [{"name": f"T{i}", "raw_text": f"T{i}", "used_in": []} for i in range(9)]
    items = ts.build_items(raw, source, limit=6)
    assert [i.name for i in items] == ["T0", "T1", "T2", "T3", "T4", "T5"]


def test_build_items_tolerates_malformed_entries() -> None:
    raw: list[Any] = [
        "Python",
        {"name": "Python"},
        {"name": None},
        {"name": "ResNet", "raw_text": "Python、PyTorch、ResNet"},
    ]
    items = ts.build_items(raw, _SOURCE)
    assert [i.name for i in items] == ["ResNet"]


def test_build_items_empty_input_is_empty_result() -> None:
    assert ts.build_items([], _SOURCE) == []


# ── 编排:短路不触网 ──────────────────────────────────


@pytest.mark.asyncio
async def test_extract_empty_text_returns_empty_without_model(monkeypatch) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("空输入不得触网")

    monkeypatch.setattr(ts, "build_model", _boom)
    assert await ts.extract_tech_stack("") == []
    assert await ts.extract_tech_stack("   ") == []


@pytest.mark.asyncio
async def test_extract_without_any_section_returns_empty_without_model(monkeypatch) -> None:
    """定位不到技术栈栏与项目栏 → 无可锚定原文,模型必被闸门全否。"""

    def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("无可锚定原文不得触网")

    monkeypatch.setattr(ts, "build_model", _boom)
    assert await ts.extract_tech_stack("自我介绍:\n爱笑\n加入理由:\n想学习") == []


def _install_fake_model(monkeypatch, payload: str) -> None:
    class _Msg:
        content = payload

    class _M:
        async def ainvoke(self, messages: list[Any]) -> Any:
            return _Msg()

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ts, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ts, "get_effective_settings", lambda: _S())


@pytest.mark.asyncio
async def test_extract_happy_path_through_model(monkeypatch) -> None:
    text = "技术栈:\nPython、PyTorch\n项目经验:\n钢材缺陷检测用了 YOLO11n"
    _install_fake_model(
        monkeypatch,
        json.dumps(
            {
                "items": [
                    {"name": "PyTorch", "raw_text": "Python、PyTorch", "claimed_level": "熟练",
                     "used_in": ["钢材缺陷检测"]},
                    {"name": "Kubernetes", "raw_text": "精通 K8s", "claimed_level": "精通",
                     "used_in": []},
                ]
            },
            ensure_ascii=False,
        ),
    )
    items = await ts.extract_tech_stack(text)
    assert [i.name for i in items] == ["PyTorch"]  # 编造的被闸门挡下
    assert items[0].claimed_level == "熟练"
    assert items[0].used_in == ("钢材缺陷检测",)


@pytest.mark.asyncio
async def test_extract_all_fabricated_returns_empty_not_error(monkeypatch) -> None:
    """模型全编造 → 空列表降级,不是异常。"""
    text = "技术栈:\nPython\n项目经验:\n做过网站"
    _install_fake_model(
        monkeypatch,
        json.dumps({"items": [{"name": "Rust", "raw_text": "精通 Rust", "used_in": []}]}),
    )
    assert await ts.extract_tech_stack(text) == []


@pytest.mark.asyncio
async def test_extract_unparsable_reply_returns_empty(monkeypatch) -> None:
    _install_fake_model(monkeypatch, "抱歉,我无法完成该请求。")
    assert await ts.extract_tech_stack("技术栈:\nPython") == []


@pytest.mark.asyncio
async def test_extract_wrong_shape_returns_empty(monkeypatch) -> None:
    _install_fake_model(monkeypatch, json.dumps({"result": "ok"}))
    assert await ts.extract_tech_stack("技术栈:\nPython") == []


@pytest.mark.asyncio
async def test_extract_model_failure_propagates(monkeypatch) -> None:
    """基础设施故障不是「抽不出技术栈」,不该被静默当成降级信号。"""

    class _M:
        async def ainvoke(self, messages: list[Any]) -> Any:
            raise RuntimeError("上游 500")

    class _S:
        model_strong = "test-strong"

    monkeypatch.setattr(ts, "build_model", lambda *a, **k: _M())
    monkeypatch.setattr(ts, "get_effective_settings", lambda: _S())
    with pytest.raises(RuntimeError, match="上游 500"):
        await ts.extract_tech_stack("技术栈:\nPython")
