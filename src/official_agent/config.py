"""集中配置:全部来自环境变量 / .env,真实凭证永不入库。"""

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

    # 模型(分级路由)。provider=anthropic 走 ANTHROPIC_API_KEY;
    # provider=openai-compatible 走 OpenAI 兼容端点(DeepSeek 等),
    # llm_api_key+llm_base_url 决定接入
    llm_provider: str = "anthropic"
    llm_base_url: str = ""
    llm_api_key: str = ""
    anthropic_api_key: str = ""
    model_light: str = "claude-haiku-4-5-20251001"  # 降档预留;当前无消费方
    model_strong: str = "claude-sonnet-5"
    # 会话压缩摘要用的模型;空 = 同 model_strong。改成便宜模型前必须先跑
    # evals/benchmarks/compress_benchmark.py 证明压缩后可答性不降(ADR-0004)
    model_compress: str = ""

    # Embedding:独立配置组,与 build_model 平行,不塞 llm_base_url——
    # 密钥只存 env 不入库,HOT_KEYS 仅收对话模型路由的键。
    # Anthropic 不提供 embedding → OpenAI-compatible 托管中文模型
    # (Qwen3-Embedding-0.6B / BGE-M3 等);具体 provider/model 落 kb_meta,
    # 换模型 = 全量 reindex + meta 版本 bump(禁跨模型向量混排)。
    embed_provider: str = ""  # 标识用(如 "openai-compatible");不影响请求构造
    embed_base_url: str = ""  # 形如 https://host/v1(自动拼 /embeddings)
    embed_api_key: str = ""
    embed_model: str = ""
    embed_dim: int = 0  # 0=不校验;>0 时与端点返回维度强校验
    # 知识库检索门槛:向量相似度低于 kb_min_score、且问题关键词字面命中比例低于
    # kb_keyword_min_ratio 的块不返回。0.45 按 Qwen3-Embedding-0.6B 实测:
    # 正例 top1 最低 0.54,明显无关的问题最高 0.45;换 embedding 模型须重校
    kb_min_score: float = 0.45
    kb_keyword_min_ratio: float = 0.5

    # 状态与记忆(ADR-0007:checkpointer/Store 均用 Postgres,Redis 退出 agent 栈)
    postgres_url: str = "postgresql://localhost:5432/official_agent"

    # 会话注册表:进程内有界;淘汰只删运行时对象,PG 档案/checkpoint 不动
    session_registry_max: int = 500
    session_registry_ttl_seconds: int = 3600

    # 单轮执行预算:墙钟超时与 LangGraph 递归上限;用户级配额另设,不在此层
    turn_wall_clock_timeout: int = 120  # 秒;建议 60–120
    turn_recursion_limit: int = 25
    # 全局活跃模型调用并发闸:跨用户资源保护;数值需与用户级配额口径统一
    model_call_global_concurrency: int = 4
    model_gate_acquire_timeout: int = 15  # 秒;闸满等待上限,超过回 busy

    # 会话保留 TTL:软删档案超期后物理清理(连带 checkpoint/对话日志)。
    # **0 = 关闭**——保留天数由数据留存 ADR 决定,ADR 落地前不启用。
    thread_retention_days: int = 0
    # GitHub 调查:调查工具层令牌,可选——匿名 60 次/时限流,
    # code search 等端点无 token 直接 401。不进 HOT_KEYS(凭证只留 .env)
    github_token: str = ""

    # FastAPI 服务:官网候选人客服通道
    agent_host: str = "127.0.0.1"
    agent_port: int = 8001
    # CORS 白名单:官网前端域名(默认 dev:React CRA http://localhost:3000)
    agent_cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # 飞书
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""

    # 观测
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # 运行时上下文:会话超阈值压缩。调优参数,冷启动生效(不入 HOT_KEYS
    # ——改它们无需重建 LLM client,仅影响下一轮压缩判定)
    context_compress_threshold_tokens: int = 24000
    context_recent_keep_messages: int = 12

    # 长期记忆(memory/):管理员与社员跨会话记住偏好与事实。每隔几轮从用户本人的
    # 发言里提取一次;召回条数;保质期(天)。memory_enabled=False 整体关闭
    memory_enabled: bool = True
    memory_extract_every_turns: int = 3
    memory_recall_limit: int = 3
    memory_ttl_days: int = 180

    # 初筛 job 后台领取循环的间隔(秒);0 = 关闭(只靠提交时派发 + 重启恢复)
    evaluation_claim_interval_seconds: int = 30

    # 评估域同时在途的模型调用上限(评分、探索、出题、审题共用一把进程级闸)。
    # 出题证据线并发、runner 多 job 并行会叠乘;8 是 runner 的 job 并发数的两倍,
    # 没有按模型供应商的限流实测过,见到 429 就调小
    evaluation_model_concurrency: int = 8

    # 审题(出题之后逐题给意见):off = 不审;annotate = 审并把意见挂在题库上,不改题;
    # enforce = 在 annotate 之上,打回的题让出题者改一次、建议丢弃的题在不破坏题组
    # 结构时丢弃。默认关:每条有题的证据线多一次 strong 调用,要先在真实模型上跑过
    # evals/datasets/question_review_probes.yaml 再打开
    qbank_review_mode: str = "off"
    # enforce 的第二道确认:只有这一项也为真,enforce 才生效,否则按 annotate 处理。
    # 置真的前提是审题者的误杀率已经达标(question_review_probes 的 good_keep_rate),
    # 这样单独把上面那项写成 enforce——抄错配置、提前改好——都不会让它开始改题
    qbank_review_enforce_ready: bool = False

    # 题目向量索引(本包跨线查重 + 跨简历通用题标注,只标注不删题)。默认关:
    # 两个相似度门槛和「几份简历算通用」都是初值,要先拿现有题库量过分布再打开;
    # 打开还需要 EMBED_* 已配置、题库所在的库装有 pgvector
    qbank_index_enabled: bool = False
    qbank_duplicate_min_similarity: float = 0.9
    qbank_generic_min_similarity: float = 0.9
    qbank_generic_min_resumes: int = 3

    # 题型范例(从面试官选过的题里归纳去标识的提问方式,人审通过后接在出题 prompt
    # 后面当范例)。默认关:关着时不归纳、管理面的写接口拒绝、出题不带范例。打开的
    # 前提是已经有至少一个完整周期的选题记录
    qbank_exemplars_enabled: bool = False

    # 规划者(给每条仓线定探索档位与侧重,预算数值由代码按档位映射):off = 不调,
    # 每条线都是最高档;plan = 调。默认关:要先在真实模型上跑过
    # evals/datasets/plan_probes.yaml 的模型段再打开
    qbank_plan_mode: str = "off"
    # 一份简历的探索预算总上限(各仓线预算之和),规划者开没开都生效。缺省值是
    # 「5 条仓线全部最高档」的总量,即不比没有上限时更紧;调小后超出的部分从
    # 最低档的线开始砍(见 evaluation/plan.py 的 cap_lines)
    qbank_explore_max_total_turns: int = 400
    qbank_explore_max_total_wall_seconds: int = 1500

    # 初筛分级:同部门候选池内头部/尾部比例与小池阈值(见 evaluation/grading.py)
    evaluation_grade_top_ratio: float = 0.2
    evaluation_grade_bottom_ratio: float = 0.2
    evaluation_grade_min_pool: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 可热载低敏键白名单:这些键可被 agent_config 表覆盖并热生效。
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
    """使 get_settings 的 lru_cache 失效(热生效)。

    调用后下次 get_settings() 重新读 .env;依赖该配置的组件
    (LLM client 等)在下次构造时自然用新值。
    """
    get_settings.cache_clear()


def get_effective_settings():
    """返回合并 DB 覆盖的 Settings(DB 低敏值优先于 env;热生效)。

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
