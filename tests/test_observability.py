"""OBS-01 观测接线单测:fail-open 三条路径 + 已配置路径。"""

import pytest

from boyuan_agent.config import Settings


def test_no_config_returns_empty_and_warns_once(caplog) -> None:
    settings = Settings(_env_file=None)  # langfuse_* 全空
    import boyuan_agent.observability as obs

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

    import boyuan_agent.observability as obs

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

    import boyuan_agent.observability as obs

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


def langfuse_callbacks_with(settings: Settings) -> list:
    import boyuan_agent.observability as obs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(obs, "get_settings", lambda: settings)
        return obs.langfuse_callbacks()
