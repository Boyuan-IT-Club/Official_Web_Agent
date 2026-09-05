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
    model_light: str = ""  # 轻量模型(压缩摘要);空 = 用 model_strong(兼容单模型部署)
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

    # 运行时上下文(M6 #114):会话超阈值压缩。调优参数,冷启动生效(不入 HOT_KEYS
    # ——改它们无需重建 LLM client,仅影响下一轮压缩判定)
    context_compress_threshold_tokens: int = 24000
    context_recent_keep_messages: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 可热载低敏键白名单(M6 #111):这些键可被 agent_config 表覆盖并热生效。
# 高敏/启动必需键(真实 key/密码/连接串/host/port)永远只在 .env,不可入库覆盖。
HOT_KEYS: frozenset[str] = frozenset(
    {
        "llm_provider",
        "llm_base_url",
        "model_light",
        "model_strong",
    }
)


def invalidate_settings_cache() -> None:
    """使 get_settings 的 lru_cache 失效(M6 #111 热生效)。

    调用后下次 get_settings() 重新读 .env;依赖该配置的组件
    (LLM client 等)在下次构造时自然用新值。
    """
    get_settings.cache_clear()


def get_effective_settings():
    """返回合并 DB 覆盖的 Settings(DB 低敏值优先于 env;#111 热生效)。

    lazy import 避免循环:config_store 依赖本模块的 get_settings。
    """
    settings = get_settings()
    try:
        from official_agent.state.config_store import get_all_config

        overrides = get_all_config()
    except Exception:  # noqa: BLE001 — PG 未起/配置错 → 用 env 原值(fail-open)
        return settings
    # 只合并白名单键;确保类型为 str
    updates = {k: v for k, v in overrides.items() if k in HOT_KEYS and isinstance(v, str)}
    return settings.model_copy(update=updates) if updates else settings
