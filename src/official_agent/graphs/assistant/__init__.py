"""A 模块 ReAct 单循环(GRA-04):按身份装配只读工具集 → create_agent。

形态(ADR-0003):无意图分类,「意图」由模型在循环内选工具隐式表达。
装配粗粒度三档(SEC-02 后续细化到权限码级并补安全测试):
- admin:9 个只读工具全量
- member:公共查询面(open_cycle/search/统计/场次容量)
- candidate:get_open_cycle + get_my_interview,且装配时绑定用户令牌——模型只见
  cycle_id,永不接触凭证(凭证红线,GRA-01 同款纪律);加 get_open_cycle 是为让
  候选人自动拿「当前开放周期」再查本人面试(候选入口无周期管理,需自己能取)
- unknown:空集(无工具,纯问答;装配层是第一道闸,ADR-0005)

静态前缀纪律(prompt cache,参考 ai-agent-book ch2):
system prompt 与工具定义保持字节级稳定、与角色无关;身份信息作为
**首条用户消息**注入(会话内稳定,跨会话不污染缓存键)。

写工具(assign_interview 等)不装配——写操作闭环是 GRA-05/M3
(interrupt 需 MEM-01 checkpointer),M1 验收为多工具组合查询。
"""

from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from official_agent.config import get_effective_settings
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.tools import readonly

_PROMPT_FILE = Path(__file__).parent.parent.parent / "prompts" / "assistant.md"

_ROLE_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "admin": (
        "get_open_cycle",
        "search_resumes",
        "get_resume_detail",
        "get_my_interview",
        "find_available_sessions",
        "list_unassigned",
        "list_reschedule_requests",
        "get_recruit_statistics",
        "get_candidate_card",
    ),
    "member": (
        "get_open_cycle",
        "search_resumes",
        "get_recruit_statistics",
        "find_available_sessions",
    ),
    "candidate": ("get_open_cycle", "get_my_interview"),
    "unknown": (),
}

_ALL_TOOLS: dict[str, object] = {
    "get_open_cycle": readonly.get_open_cycle,
    "search_resumes": readonly.search_resumes,
    "get_resume_detail": readonly.get_resume_detail,
    "get_my_interview": readonly.get_my_interview,
    "find_available_sessions": readonly.find_available_sessions,
    "list_unassigned": readonly.list_unassigned,
    "list_reschedule_requests": readonly.list_reschedule_requests,
    "get_recruit_statistics": readonly.get_recruit_statistics,
    "get_candidate_card": readonly.get_candidate_card,
}


def load_system_prompt() -> str:
    """读 prompts/assistant.md,剥离 frontmatter,返回正文。

    prompt 唯一权威是文件(ADR-0004);正文与角色无关(静态前缀纪律)。
    """
    text = _PROMPT_FILE.read_text(encoding="utf-8")
    if text.startswith("---"):
        _, _, body = text.split("---", 2)
        return body.strip()
    return text.strip()


def _bind_my_interview(user_token: str):
    """闭包绑定用户令牌:模型侧签名只剩 cycle_id,凭证不可见。

    闭包而非 functools.partial:LangChain 工具转换依赖 __name__/__doc__,
    partial 对象两者皆缺。docstring 从原函数继承,工具描述不丢。
    """

    async def get_my_interview(cycle_id: int) -> dict | None:
        return await readonly.get_my_interview(cycle_id, user_token=user_token)

    get_my_interview.__doc__ = readonly.get_my_interview.__doc__
    return get_my_interview


def assemble_tools(identity: ResolvedIdentity, user_token: str = "") -> list:
    """按 role 装配工具集(粗粒度三档)。

    get_my_interview 需要用户本人令牌:装配时闭包绑定,模型侧签名只剩
    cycle_id——凭证不进模型可见面。
    """
    names = _ROLE_TOOL_NAMES.get(identity.get("role", "unknown"), ())
    tools = []
    for name in names:
        if name == "get_my_interview":
            tools.append(_bind_my_interview(user_token))
        else:
            tools.append(_ALL_TOOLS[name])
    return tools


def identity_message(identity: ResolvedIdentity) -> str:
    """身份注入文案:作为首条用户消息(动态信息不进 system)。

    只含对话需要的档案:称呼、职位、权限边界。不含 user_id/source 等内部
    标识(用户明确要求)——agent 面对用户时应像面对一个人,不暴露内部字段。
    """
    role_label = {
        "admin": "管理员",
        "member": "社员",
        "candidate": "候选人",
        "unknown": "用户",
    }.get(identity.get("role", "unknown"), "用户")
    name = identity.get("name")
    greeting = f"{name}同学" if name and identity.get("role") == "candidate" else (
        name or role_label
    )
    perms = identity.get("permission_codes") or []
    perm_note = (
        f",可访问权限: {', '.join(perms[:6])}{' 等' if len(perms) > 6 else ''}"
        if perms
        else ",当前无额外数据访问权限"
    )
    return (
        f"当前对话用户是{greeting}({role_label})。"
        f"{perm_note}。"
        "回答时用自然称呼,不要提及内部字段。"
    )


def _build_model(settings: Any) -> Any:
    """按配置构造对话模型(GRA-08 路由的接入点)。

    - anthropic:ANTHROPIC_API_KEY(默认)
    - openai-compatible:OpenAI 兼容端点(DeepSeek 等),LLM_BASE_URL+
      LLM_API_KEY——换模型供应商不改代码
    """
    if settings.llm_provider == "openai-compatible":
        from langchain_openai import ChatOpenAI

        if not settings.llm_base_url or not settings.llm_api_key:
            raise ValueError(
                "openai-compatible 模式需要在 .env 配置 LLM_BASE_URL 与 LLM_API_KEY"
            )
        return ChatOpenAI(
            model=settings.model_strong,
            api_key=SecretStr(settings.llm_api_key),
            base_url=settings.llm_base_url,
        )
    # ChatAnthropic 为 pydantic **kwargs 构造器,mypy 无法静态解析字段
    return ChatAnthropic(  # type: ignore[call-arg]
        model=settings.model_strong,
        api_key=SecretStr(settings.anthropic_api_key) if settings.anthropic_api_key else None,  # type: ignore[arg-type]
    )


def build_assistant_agent(
    identity: ResolvedIdentity,
    user_token: str = "",
    checkpointer: Any | None = None,
) -> Any:
    """构建 A 模块 ReAct agent。调用方(CLI/SSE)负责身份解析与消息装配,
    并把 langfuse_callbacks 挂到 invoke 的 config(fail-open,ADR-0005)。

    checkpointer(MEM-01):传 AsyncPostgresSaver 则启用多轮持久化;
    None 则纯内存(CLI --session 标识仅作 trace 用)。"""
    settings = get_effective_settings()
    return create_agent(
        _build_model(settings),
        tools=assemble_tools(identity, user_token),
        system_prompt=load_system_prompt(),
        checkpointer=checkpointer,
    )
