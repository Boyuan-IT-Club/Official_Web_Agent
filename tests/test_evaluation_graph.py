"""B1 评分子图测试:硬 0 短路不调模型 / 正常路径结构化输出 / 失败进 error。"""

import asyncio
from unittest.mock import patch

import pytest

from official_agent.evaluation import graph as ev

_FIELDS = [
    {"field_key": "intro", "title": "自我介绍", "value": "我是张三,做过两个 Web 项目。"},
    {"field_key": "reason", "title": "加入理由", "value": "认同社团氛围,想参与招新开发。"},
]

_WEIGHTS = {"intro": 3.0, "reason": 1.0}


def _fields_all_bad() -> list[dict[str, str]]:
    return [
        {"field_key": "intro", "title": "自我介绍", "value": "111"},
        {"field_key": "reason", "title": "加入理由", "value": "请输入加入理由"},
    ]


class _FakeMsg:
    def __init__(self, content):
        self.content = content


def _fake_model(payload: str):
    class _M:
        async def ainvoke(self, messages, config=None):
            return _FakeMsg(payload)

    return _M()


_GOOD_JSON = (
    '{"dimensions": ['
    '{"field_key": "intro", "score": 80, "rationale": "具体", "evidence": "做过两个 Web 项目"},'
    '{"field_key": "reason", "score": 40, "rationale": "偏短", "evidence": "认同社团氛围"}],'
    '"attitude": {"verdict": "sincere", "reason": "认真"}}'
)


def _settings():
    class _S:
        model_strong = "test-strong"

    return _S()


@pytest.mark.asyncio
async def test_hard_zero_short_circuits_without_model() -> None:
    """任一维绝对卡 → 整份硬 0,不调模型(#123 确定性规则优先)。"""

    def _boom(*a, **k):
        raise AssertionError("硬 0 路径不得调模型")

    with (
        patch.object(ev, "build_model", _boom),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        card = await ev.run_evaluation(
            _fields_all_bad(), resume_id=1, cycle_id=2026, weights=_WEIGHTS
        )
    assert card["hard_zero"] is True
    assert card["total"] == 0.0
    assert card["attitude"]["verdict"] == "bad_faith"
    assert all(d["score"] == 0 for d in card["dimensions"])
    assert "单字符重复" in json_of_reasons(card)
    assert card["versions"]["prompt"] == "evaluation_scoring/v3"


def json_of_reasons(card: dict) -> str:
    return str(card["hard_zero_reasons"])


@pytest.mark.asyncio
async def test_normal_path_scores_and_weights_total() -> None:
    with (
        patch.object(ev, "build_model", lambda *a, **k: _fake_model(_GOOD_JSON)),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        card = await ev.run_evaluation(_FIELDS, resume_id=2, cycle_id=2026, weights=_WEIGHTS)
    assert card["hard_zero"] is False
    assert card["total"] == 70.0  # (80×3 + 40×1) / 4
    assert card["attitude"]["verdict"] == "sincere"
    assert card["dimensions"][0]["evidence"] == "做过两个 Web 项目"
    assert card["schema"] == "evaluation_scorecard/v1"


@pytest.mark.asyncio
async def test_llm_failure_lands_in_error() -> None:
    class _Boom:
        async def ainvoke(self, messages):
            return _FakeMsg("模型打摆了,没有 JSON")

    with (
        patch.object(ev, "build_model", lambda *a, **k: _Boom()),
        patch.object(ev, "get_effective_settings", _settings),
        pytest.raises(RuntimeError, match="评分子图失败"),
    ):
        await ev.run_evaluation(_FIELDS, resume_id=3, cycle_id=2026)


@pytest.mark.asyncio
async def test_temperature_low_on_scorer() -> None:
    """评分走低温档(#123:model_strong+低温)。"""
    captured: dict = {}

    def _fake_build(settings, model=None, stream_usage=False, temperature=None):
        captured["temperature"] = temperature
        return _fake_model(
            '{"dimensions": [{"field_key": "intro", "score": 50, '
            '"rationale": "r", "evidence": "e"}],'
            '"attitude": {"verdict": "sincere", "reason": "r"}}'
        )

    with (
        patch.object(ev, "build_model", _fake_build),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        await ev.run_evaluation(_FIELDS[:1], resume_id=4, cycle_id=2026)
    assert captured["temperature"] == ev.SCORING_TEMPERATURE


def test_extract_json_tolerates_fences_and_noise() -> None:
    """评审 P2:围栏/前导杂文/尾随杂文都能截出 JSON 主体。"""
    assert ev._extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert ev._extract_json('好的,以下是结果:\n{"a": 1}') == '{"a": 1}'
    tail = '以下是结果:\n{"a": 1}\n注:权重仅供参考}'
    assert ev._extract_json(tail) == '{"a": 1}'  # raw_decode:尾随杂文不进 JSON
    with pytest.raises(ValueError, match="不含 JSON"):
        ev._extract_json("模型打摆了,没有 JSON")


def test_dimension_incompleteness_raises() -> None:
    """评审 P1-1:模型漏维 → error 态,绝不落'看起来完整'的卡。"""
    bad = (
        '{"dimensions": [{"field_key": "intro", "score": 80, '
        '"rationale": "r", "evidence": "做过两个 Web 项目"}],'
        '"attitude": {"verdict": "sincere", "reason": "r"}}'
    )
    with (
        patch.object(ev, "build_model", lambda *a, **k: _fake_model(bad)),
        patch.object(ev, "get_effective_settings", _settings),
        pytest.raises(RuntimeError, match="维度集不完整"),
    ):
        asyncio.run(ev.run_evaluation(_FIELDS, resume_id=5, cycle_id=2026))


def test_fabricated_evidence_raises() -> None:
    """评审 P1-2:证据非原文 → error 态(编造证据不得落卡)。"""
    bad = (
        '{"dimensions": ['
        '{"field_key": "intro", "score": 80, "rationale": "r", "evidence": "我获得过图灵奖"},'
        '{"field_key": "reason", "score": 40, "rationale": "r", "evidence": "认同社团氛围"}],'
        '"attitude": {"verdict": "sincere", "reason": "r"}}'
    )
    with (
        patch.object(ev, "build_model", lambda *a, **k: _fake_model(bad)),
        patch.object(ev, "get_effective_settings", _settings),
        pytest.raises(RuntimeError, match="证据非原文"),
    ):
        asyncio.run(ev.run_evaluation(_FIELDS, resume_id=6, cycle_id=2026))


@pytest.mark.asyncio
async def test_placeholder_flows_into_hard_zero() -> None:
    """B2 评审 P1:placeholder 必须进绝对卡判定(端到端通路)。"""
    fields = [
        {
            "field_key": "intro",
            "title": "自我介绍",
            "value": "介绍一下你参与过的项目、承担的角色和最终成果",  # 抄配置 placeholder
            "placeholder": "介绍一下你参与过的项目、承担的角色和最终成果",
        },
        {
            "field_key": "reason",
            "title": "加入理由",
            "value": "请描述你印象最深的协作:大二时我组织过校际联调试。",
            "placeholder": "说说你为什么想加入",
        },
    ]
    card = await ev.run_evaluation(fields, resume_id=7, cycle_id=2026)
    assert card["hard_zero"] is True
    # intro 命中 placeholder 全等;reason 有配置 placeholder 时前缀启发式不启用 → 不卡
    assert "placeholder 文案未改" in str(card["hard_zero_reasons"])
    # 有配置 placeholder 的 reason:前缀启发式不启用,抄题开头不作卡
    assert "reason" not in card["hard_zero_reasons"]


@pytest.mark.asyncio
async def test_all_zero_llm_card_marks_hard_zero() -> None:
    """B2 评审 P1:AI 全 0 分卡也要落 hard_zero(0 分队列靠它捞)。"""
    payload = (
        '{"dimensions": ['
        '{"field_key": "intro", "score": 0, "rationale": "r",'
        ' "evidence": "我是张三,做过两个 Web 项目。"},'
        '{"field_key": "reason", "score": 0, "rationale": "r",'
        ' "evidence": "认同社团氛围,想参与招新开发。"}],'
        '"attitude": {"verdict": "bad_faith",'
        ' "reason": "reason 维整份无实质内容,原文引述:(空白)"}}'
    )
    with (
        patch.object(ev, "build_model", lambda *a, **k: _fake_model(payload)),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        card = await ev.run_evaluation(_FIELDS, resume_id=8, cycle_id=2026)
    assert card["hard_zero"] is True
    assert card["total"] == 0.0


# ── #183:评分 token 经 usage_out 回传 + correlation_id 进 config metadata ──


class _UsageMsg:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage_metadata = {
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        }


def _usage_model(payload: str):
    class _M:
        async def ainvoke(self, messages, config=None):
            return _UsageMsg(payload)

        def with_structured_output(self, schema, **kwargs):  # pragma: no cover
            raise NotImplementedError

    return _M()


@pytest.mark.asyncio
async def test_scoring_usage_flows_to_usage_out() -> None:
    """#183 P1:llm_score 的 usage 经节点返回值 → run_evaluation usage_out。"""
    usage_out: dict = {}

    def _model(*a, **k):
        return _usage_model(_GOOD_JSON)

    with (
        patch.object(ev, "build_model", _model),
        patch.object(ev, "get_effective_settings", _settings),
    ):
        await ev.run_evaluation(
            _FIELDS,
            resume_id=11,
            cycle_id=2026,
            usage_out=usage_out,
            correlation_id="corr-test-123",
        )
    assert usage_out.get("input_tokens") == 120
    assert usage_out.get("output_tokens") == 30


@pytest.mark.asyncio
async def test_correlation_id_lands_in_root_metadata() -> None:
    """#183 P2:correlation_id 经根 run config.metadata 进回调(Langfuse 关联键)。"""
    from langchain_core.callbacks import BaseCallbackHandler

    seen: dict = {}

    class _RecordingHandler(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, *, metadata=None, **kwargs):
            seen.setdefault("metadata", metadata or {})

    class _M:
        async def ainvoke(self, messages, config=None):
            return _UsageMsg(_GOOD_JSON)

    with (
        patch.object(ev, "build_model", lambda *a, **k: _M()),
        patch.object(ev, "get_effective_settings", _settings),
        patch("official_agent.observability.langfuse_callbacks", lambda: [_RecordingHandler()]),
    ):
        await ev.run_evaluation(
            _FIELDS, resume_id=12, cycle_id=2026, correlation_id="corr-abc"
        )
    assert seen["metadata"].get("correlation_id") == "corr-abc"
