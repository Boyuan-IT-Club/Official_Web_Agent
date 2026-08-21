"""集中配置(INF-02):全部来自环境变量 / .env,真实凭证永不入库。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 后端
    backend_base_url: str = "http://localhost:8080"
    backend_service_username: str = ""
    backend_service_password: str = ""

    # 模型(分级路由,GRA-08)
    anthropic_api_key: str = ""
    model_light: str = "claude-haiku-4-5-20251001"
    model_strong: str = "claude-sonnet-5"

    # 记忆
    redis_url: str = "redis://localhost:6379/1"
    postgres_url: str = ""  # M4 起启用

    # 飞书(M3)
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # 观测
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
