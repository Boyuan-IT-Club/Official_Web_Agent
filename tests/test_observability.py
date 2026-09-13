"""OBS-01 观测接线单测:fail-open 三条路径 + 已配置路径。"""

import pytest

from official_agent.config import Settings


def test_no_config_returns_empty_and_warns_once(caplog) -> None:
    settings = Settings(_env_file=None)  # langfuse_* 全空
    import official_agent.observability as obs

    obs._warned_no_config = False
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(obs, "get_settings", lambda: settings)
        assert obs.langfuse_callbacks() == []
        assert obs.langfuse_callbacks() == []  # 第二次不再重复告警
    warns = [r for r in caplog.records if "Langfuse 未配置" in r.message]
    assert len(warns) == 1


def test_configured_returns_handler() -> None:
    settings = Settings(
        _env_file=None,
        langfuse_host="http://localhost:3001",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    callbacks = langfuse_callbacks_with(settings)
    assert len(callbacks) == 1
    # 真构造 Langfuse client(handler 内部从全局单例取),不触发网络请求
    assert callbacks[0] is not None


def test_build_failure_degrades_to_empty(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        langfuse_host="http://localhost:3001",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )

    def boom(_settings):  # noqa: ANN001 — 模拟 SDK 导入/构造失败
        raise RuntimeError("sdk exploded")

    import official_agent.observability as obs

    monkeypatch.setattr(obs, "_build_handler", boom)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(obs, "get_settings", lambda: settings)
        assert obs.langfuse_callbacks() == []  # fail-open:吞掉,降级为不上报


def test_build_handler_uses_settings(monkeypatch) -> None:
    """_build_handler 把三件凭证交给全局 client——防字段名漂移。"""
    captured = {}

    class FakeLangfuse:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import official_agent.observability as obs

    monkeypatch.setattr("langfuse.Langfuse", FakeLangfuse, raising=False)
    import langfuse

    monkeypatch.setattr(langfuse, "Langfuse", FakeLangfuse, raising=True)
    settings = Settings(
        _env_file=None,
        langfuse_host="http://lf",
        langfuse_public_key="pk-x",
        langfuse_secret_key="sk-x",
    )
    obs._build_handler(settings)
    assert captured == {
        "public_key": "pk-x",
        "secret_key": "sk-x",
        "host": "http://lf",
    }


# ── #194 复审 P1:PII 包装 fail-closed ──


class _RecordingInner:
    """内层 handler 替身:记录真实收到的载荷(断言原文永不流到内层)。"""

    def __init__(self) -> None:
        self.received: list[object] = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.received.append(messages)
        return None

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.received.append(inputs)
        return None

    def on_tool_end(self, output, **kwargs):
        self.received.append(output)
        return None


class _BoomMessage:
    """content 含 PII 且 model_copy 抛错的消息:不能上报原文。"""

    content = "phone 13800138000"

    def model_copy(self, **_kw):
        raise RuntimeError("copy boom")


def test_pii_handler_drops_message_when_copy_fails(caplog) -> None:
    """#194 P1:构造不出脱敏副本 → 丢弃该条;内层永远拿不到原值。"""
    import official_agent.observability as obs

    inner = _RecordingInner()
    h = obs._PiiMaskedLangfuseHandler(inner)
    with caplog.at_level("WARNING"):
        h.on_chat_model_start({}, [[_BoomMessage()]])
    assert inner.received == [], "全部无法脱敏时应跳过上报,而不是回落原值"


def test_pii_handler_redacts_beyond_max_depth() -> None:
    """#194 P1:遍历超深 → 定值占位,不回传原值。"""
    import official_agent.observability as obs

    h = obs._PiiMaskedLangfuseHandler(_RecordingInner())
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "13800138000"}}}}}}}
    masked = h._mask_payload(deep)
    assert "13800138000" not in str(masked), "深层载荷不得原样回传"
    assert obs._PiiMaskedLangfuseHandler._REDACTED in str(masked)


def test_pii_handler_redacts_llmresult_generations() -> None:
    """#194 P1:LLMResult 之类不直接暴露 .content 的对象 → 受控投影。

    旧行为原样委托,嵌套 generations 里的手机号会绕过掩码进 trace。
    """
    import official_agent.observability as obs

    class _Gen:
        def __init__(self) -> None:
            self.text = "13800138000"

    class _LLMResult:
        generations = [[_Gen()]]

    h = obs._PiiMaskedLangfuseHandler(_RecordingInner())
    masked = h._mask_payload(_LLMResult())
    assert "13800138000" not in str(masked)
    assert masked == obs._PiiMaskedLangfuseHandler._REDACTED


def test_pii_handler_still_masks_plain_payload() -> None:
    """回归:正常路径仍做确定性掩码(不因 fail-closed 收紧而漏掩)。"""
    import official_agent.observability as obs

    h = obs._PiiMaskedLangfuseHandler(_RecordingInner())
    assert h._mask_payload("call 13800138000") == "call 138****8000"
    assert h._mask_payload({"phone": "13800138000"}) == {"phone": "138****8000"}


def langfuse_callbacks_with(settings: Settings) -> list:
    import official_agent.observability as obs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(obs, "get_settings", lambda: settings)
        return obs.langfuse_callbacks()
