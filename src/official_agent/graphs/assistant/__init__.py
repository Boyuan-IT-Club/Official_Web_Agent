"""A 模块 ReAct 单循环(GRA-04):按身份装配只读工具集 → create_agent。

形态(ADR-0003):无意图分类,「意图」由模型在循环内选工具隐式表达。
装配须与后端 RBAC 对齐(ADR-0006「社团官网层问答助手」):不能只按粗粒度
role 给工具,否则会把后端 V22 已从社员角色撤下(如 resume:view)的越权面
经 query tool 原样露出。**收口后的目标装配(SEC-02 落地,本文件底部
`_ROLE_TOOL_NAMES`/装配表仍是旧 role 分档,是已知遗留)**:
- admin:只读工具集全量(以本人 JWT get_as_user 调后端,后端复核)
- member:全局(跨人)查询工具一律不装配 —— member 不再有 resume:view(V22
  已撤),search_resumes 等绝不借 svc-agent 代读;只装本人真实持码的只读。
- candidate:只装本人生周期只读(get_open_cycle + get_my_interview;get_open_cycle
  供本人取当前开放周期再查本人面试),查询绑定本人 JWT,模型侧不见凭证。
- unknown:空集(无工具,纯问答;装配层是第一道闸,ADR-0005)

只读通道决断(2026-09-09):本问答助手(官网对话 web 通道)**只读、不装配
任何写工具** —— 写操作闭环是 GRA-05/M3(interrupt 需 MEM-01 checkpointer),
不在本助手能力面内;后续亦暂不扩展写工具。

system prompt 纪律(#166 后修订):
- 静态正文(prompts/assistant.md)与角色无关、字节稳定;
- 身份/权限段 + 工具契约随角色变化,进 **system prompt**(#166 修复——
  原实现把它们塞进首条用户消息,被 checkpointer 当对话原文存下、经历史
  回看投影成 user 气泡泄漏给使用者);
- 代价:prompt cache 从「全局稳定」降为「按角色分档命中」(同角色会话内
  仍稳定),prefix_hash(load_system_prompt(), tool_names) 作为基准证据不变。
"""

from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from official_agent.config import get_effective_settings
from official_agent.graphs.identity import ResolvedIdentity
from official_agent.security.injection_guard import mount_input_guard
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

    prompt 唯一权威是文件(ADR-0004);正文与角色无关(静态基准)。
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
        raw = _bind_my_interview(user_token) if name == "get_my_interview" else _ALL_TOOLS[name]
        # GRA-04/#163:工具返回出口统一过注入守卫(数据区标签+确定性扫描)
        tools.append(mount_input_guard(raw))
    return tools


def identity_message(identity: ResolvedIdentity) -> str:
    """身份/权限段落:进 system prompt(#166 修复)。

    只含对话需要的档案:称呼、职位、权限边界。不含 user_id/source 等内部
    标识(用户明确要求)——agent 面对用户时应像面对一个人。

    #166 前这段作为首条**用户消息**注入,被 checkpointer 当对话原文存下、
    经历史回看投影成角色为 user 的气泡泄漏给使用者;现改为 system message,
    与对话消息彻底分离。
    """
    role_label = {
        "admin": "管理员",
        "member": "社员",
        "candidate": "候选人",
        "unknown": "用户",
    }.get(identity.get("role", "unknown"), "用户")
    name = identity.get("name")
    greeting = (
        f"{name}同学" if name and identity.get("role") == "candidate" else (name or role_label)
    )
    perms = identity.get("permission_codes") or []
    perm_note = (
        f",可访问权限: {', '.join(perms[:6])}{' 等' if len(perms) > 6 else ''}"
        if perms
        else ",当前无额外数据访问权限"
    )
    return (
        f"当前对话用户是{greeting}({role_label})。{perm_note}。回答时用自然称呼,不要提及内部字段。"
    )


def tool_roster(identity: ResolvedIdentity) -> list[str]:
    """当前身份装配到的工具名清单(unknown 档为空)。"""
    return list(_ROLE_TOOL_NAMES.get(identity.get("role", "unknown"), ()))


def tool_contract(identity: ResolvedIdentity) -> str:
    """工具契约段(GRA-04,#161):工具清单随角色变化,进 system prompt。

    - 有工具档:列清单,只准陈述工具真实返回的内容,失败/被拒必须如实
      说明并引导人工,不得编造;
    - 空工具档(unknown):明令不得声称查询过任何数据(编造守卫第一层,
      输出守卫 fabrication_guard 是第二层兜底)。

    名单单源 tool_roster(_ROLE_TOOL_NAMES);装配过滤演化时两者仍须一致
    (test_tool_roster_matches_assembly 钉住)。
    """
    names = tool_roster(identity)
    if not names:
        return (
            "你没有可用的数据查询工具:绝不能声称查询过任何数据、给出「查询结果」"
            "或编造数据。用户要求数据时,说明你当前没有查询权限,"
            "引导其通过管理端或人工渠道获取。"
        )
    return (
        f"本次会话可用的数据查询工具:{', '.join(names)}。"
        "只陈述这些工具真实返回的内容;工具没有返回的信息,不要声称「查询过」。"
        "工具调用失败或被拒绝时,如实说明无法查询并引导人工渠道,不要编造结果。"
    )


def build_system_prompt(identity: ResolvedIdentity) -> str:
    """完整 system prompt = 静态正文 + 身份段 + 工具契约(#166)。

    身份/契约随角色变化 ⇒ system 前缀不再跨角色字节稳定,prompt cache
    按角色分档命中(同角色会话内仍稳定)。这是 #166 的取舍:内部上下文
    绝不进对话消息面,代价是缓存粒度从「全局」降为「按角色」。
    """
    return f"{load_system_prompt()}\n\n{identity_message(identity)}\n\n{tool_contract(identity)}"


def compose_first_message(user_token: str = "") -> str:
    """首条用户消息(#166 后:身份/契约已入 system,此入口只作兼容)。

    #166 前 compose_first_message(identity) 把身份段+工具契约拼成首条
    用户消息,被 checkpointer 当对话原文存下、经回看投影泄漏。现已无内部
    字段走消息面;调用方(CLI/SSE)直接以用户原文开轮即可。
    """
    del user_token  # 凭证由 asker_scope/工具闭包承载,不进消息面
    return "开始对话。"


def build_model(
    settings: Any,
    model: str | None = None,
    stream_usage: bool = False,
    temperature: float | None = None,
) -> Any:
    """按配置构造对话模型(GRA-08 路由的接入点)。

    - anthropic:ANTHROPIC_API_KEY(默认)
    - openai-compatible:OpenAI 兼容端点(DeepSeek 等),LLM_BASE_URL+
      LLM_API_KEY——换模型供应商不改代码
    model 缺省用 model_strong(对话主模型);其他用途(压缩摘要等)显式传名。
    stream_usage:流式请求附带 usage 终块(#113 用量自采)——DeepSeek/OpenAI
    兼容端点必须显式 stream_options.include_usage,且该参数只能随 stream=true
    使用(非流式 ainvoke 会 400),故只给对话主模型开;摘要器等 ainvoke 调用
    方保持缺省 False。
    temperature:低温档(结构化评分等确定性任务用 0.1 级);缺省不动原行为。
    """
    model = model or settings.model_strong
    if settings.llm_provider == "openai-compatible":
        from langchain_openai import ChatOpenAI

        if not settings.llm_base_url or not settings.llm_api_key:
            raise ValueError("openai-compatible 模式需要在 .env 配置 LLM_BASE_URL 与 LLM_API_KEY")
        return ChatOpenAI(
            model=model,
            api_key=SecretStr(settings.llm_api_key),
            base_url=settings.llm_base_url,
            model_kwargs=({"stream_options": {"include_usage": True}} if stream_usage else {}),
            **({"temperature": temperature} if temperature is not None else {}),
        )
    # ChatAnthropic 为 pydantic **kwargs 构造器,mypy 无法静态解析字段
    return ChatAnthropic(  # type: ignore[call-arg]
        model=model,
        api_key=SecretStr(settings.anthropic_api_key) if settings.anthropic_api_key else None,  # type: ignore[arg-type]
        **({"temperature": temperature} if temperature is not None else {}),
    )


def build_assistant_agent(
    identity: ResolvedIdentity,
    user_token: str = "",
    checkpointer: Any | None = None,
    stream_usage: bool = True,
) -> Any:
    """构建 A 模块 ReAct agent。调用方(CLI/SSE)负责身份解析与消息装配,
    并把 langfuse_callbacks 挂到 invoke 的 config(fail-open,ADR-0005)。

    system prompt = 静态正文 + 身份段 + 工具契约(#166,身份进 system 不再
    泄漏进对话消息面)。

    checkpointer(MEM-01):传 AsyncPostgresSaver 则启用多轮持久化;
    None 则纯内存(CLI --session 标识仅作 trace 用)。
    stream_usage:openai-compatible 端点只许 stream_options 随 stream=true 出现,
    非流式 ainvoke 调用方(eval runner #148)必须传 False。"""
    settings = get_effective_settings()
    return create_agent(
        build_model(settings, stream_usage=stream_usage),
        tools=assemble_tools(identity, user_token),
        system_prompt=build_system_prompt(identity),
        checkpointer=checkpointer,
    )
