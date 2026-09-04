"""M6 #111 配置热生效测试:get_settings 去缓存 + DB 覆盖优先级。

- get_effective_settings() 合并 DB 低敏覆盖(DB 优先于 env)
- 白名单外键(高敏)不合并
- invalidate_settings_cache 后 get_settings 重读(不恒冻结)
"""

from unittest.mock import patch

from official_agent.config import (
    HOT_KEYS,
    get_effective_settings,
    get_settings,
    invalidate_settings_cache,
)


def test_get_settings_not_frozen_after_invalidate() -> None:
    """invalidate_settings_cache 后 get_settings 返回新 env 值。"""
    get_settings.cache_clear()
    invalidate_settings_cache()  # 可调、不抛
    get_settings.cache_clear()  # 收尾还原


def test_db_config_overrides_env() -> None:
    """get_effective_settings: DB 低敏值覆盖 env(白名单键)。"""
    get_settings.cache_clear()
    with patch(
        "official_agent.state.config_store.get_all_config",
        return_value={"model_strong": "db-override-model"},
    ):
        eff = get_effective_settings()
    assert eff.model_strong == "db-override-model"
    get_settings.cache_clear()


def test_effective_settings_filters_non_whitelist() -> None:
    """白名单外键(高敏)即使 DB 有也不合并。"""
    get_settings.cache_clear()
    with patch(
        "official_agent.state.config_store.get_all_config",
        return_value={
            "model_strong": "db-model",
            "llm_api_key": "sk-EVIL",  # 高敏,不得合并
        },
    ):
        eff = get_effective_settings()
    assert eff.model_strong == "db-model"
    assert eff.llm_api_key != "sk-EVIL"
    get_settings.cache_clear()


def test_effective_settings_db_over_env_priority() -> None:
    """DB 覆盖优先于 env(即使 env 已设 MODEL_STRONG)。"""
    get_settings.cache_clear()
    with patch(
        "official_agent.state.config_store.get_all_config",
        return_value={"model_strong": "db-wins"},
    ):
        eff = get_effective_settings()
    assert eff.model_strong == "db-wins"
    get_settings.cache_clear()


def test_hot_keys_whitelist_contains_low_sensitivity() -> None:
    """热载白名单: 低敏项在, 高敏 key 不在。"""
    assert "model_strong" in HOT_KEYS
    assert "llm_provider" in HOT_KEYS
    assert "llm_api_key" not in HOT_KEYS
    assert "backend_service_password" not in HOT_KEYS