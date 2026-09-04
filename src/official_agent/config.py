"""集中配置(INF-02):全部来自环境变量 / .env,真实凭证永不入库。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 后端
    backend_base_url: str = "http://localhost:8080"
    backend_service_username: str = ""
    backend_service_password: str = ""

    # 凭证存储位置(可 env CREDENTIALS_DIR 覆盖;测试指向临时目录)
    credentials_dir: str = "~/.official-agent"

    # 模型(分级路由,GRA-08)。provider=anthropic 走 ANTHROPIC_API_KEY;
    # provider=openai-compatible 走 OpenAI 兼容端点(DeepSeek 等),
    # llm_api_key+llm_base_url 决定接入
    llm_provider: str = "anthropic"
    llm_base_url: str = ""
    llm_api_key: str = ""
    anthropic_api_key: str = ""
    model_light: str = "claude-haiku-4-5-20251001"
    model_strong: str = "claude-sonnet-5"

    # 状态与记忆(ADR-0007:checkpointer/Store 均用 Postgres,Redis 退出 agent 栈)
    postgres_url: str = "postgresql://localhost:5432/official_agent"

    # FastAPI 服务(INF-04):官网候选人客服通道
    agent_host: str = "127.0.0.1"
    agent_port: int = 8001
    # CORS 白名单:官网前端域名(默认 dev:React CRA http://localhost:3000)
    agent_cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

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
