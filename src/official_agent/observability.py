"""观测接线:LangGraph → Langfuse,fail-open。

用法(图入口):
    callbacks = langfuse_callbacks()
    result = await graph.ainvoke(state, config={"callbacks": callbacks})

fail-open 语义(ADR-0005):观测挂了不许影响主流程——
- 未配置(缺 host/key)→ 返回 [],打一次 WARNING,不打扰每次调用;
- 构造异常 → 捕获降级为 []。
写路径是 fail-closed,观测是 fail-open,两者方向相反,别混。

红线联动:本接线会把工具返回原文上报 trace。get_resume_detail
返回完整简历(含 PII)——在确定「脱敏下沉工具层 vs 采集点二次脱敏」之前,
含 PII 的工具经本 handler 上报即落 Langfuse 库,接入评估流水线前必须先解决。

prompt 版本对比(ADR-0004):prompt 唯一权威是 prompts/ 文件 frontmatter,
Langfuse 只读镜像;同步脚本待 prompt 体系落地后补。
"""

# ── PII 红线(出口契约)───────────────────────────────────────
# trace 上报面(Langfuse callbacks)的数据前提:进模型上下文的工具返回
# 已在工具层 mask_pii_deep 就地脱敏;**完整简历原文类
# payload 禁入 trace**——新增上报字段前必须先过 security/pii.py 出口
# 契约表,缺一即红线。

import contextvars
import hashlib
import logging
import re
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from official_agent.config import get_settings

logger = logging.getLogger(__name__)

_warned_no_config = False
_HEX32_RE = re.compile(r"[0-9a-f]{32}")

# 兜底值:全零 32-hex。合法 W3C trace-id 段的"显式无效"形式,
# 两侧日志见到它即知该调用发生在任何对话上下文之外。
_ZERO_TRACE_ID = "0" * 32

# 轮级 trace id:宿主(CLI/飞书/SSE 入口)每轮 set。
# 值经 set_turn_trace_id 归一为合法 W3C trace-id(32 位小写 hex),
# header 与审计共用同一归一值,保证跨端对账同 id。
_turn_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("turn_trace_id", default="")


def to_w3c_trace_id(value: str) -> str:
    """任意串 → 合法 W3C trace-id 段(32 位小写 hex)。

    已是 32 位小写 hex 原样通过;否则 sha256 确定性映射(同值同像,两侧可复算)。
    空串保持空(= 未设置,由 current_trace_id 的全零兜底接管)。
    """
    v = value.lower()
    if not v or _HEX32_RE.fullmatch(v):
        return v
    # W3C trace-id 段必须 32 位 hex:sha256 截断前 32 位(原实现用 hexdigest()
    # 全长 64 位,严格消费端会丢弃非法头,对账失效,故此截断)
    return hashlib.sha256(v.encode()).hexdigest()[:32]


def set_turn_trace_id(turn_id: str) -> contextvars.Token[str]:
    """宿主每轮对话开头调用;用返回的 token 在轮末 reset_turn_trace_id 复位。

    非 32-hex 值(如 thread_id `cli:u123:8f3a9c2b`)确定性映射为 32-hex:
    W3C 规定 trace-id 段必须 32 位小写 hex,严格消费端会丢弃非法头并自生成
    id,对账即失效。原值可读性由入口日志自行打印,不依赖此字段。
    """
    return _turn_trace_id.set(to_w3c_trace_id(turn_id))


def reset_turn_trace_id(token: contextvars.Token[str]) -> None:
    _turn_trace_id.reset(token)


def current_trace_id() -> str:
    """当前 trace id(ADR-0006 审计 trace_id 字段的取值语义),永非空。

    优先级:
    1. 活跃 span 的 32-hex trace id —— 图执行内且 Langfuse 上报生效时,
       一轮 graph invoke = 一个 trace,同轮所有后端调用共享同一 id;
    2. 轮级 contextvar(set_turn_trace_id)—— 无观测组件时兜底对话级对账;
    3. 全零 32-hex —— 无任何上下文(如登录预热、脚本直调)的确定性兜底。

    fail-open(ADR-0005):span 读取任何异常都吞掉降级,观测故障绝不打断请求路径。
    """
    try:
        span_tid = _active_span_trace_id()
    except Exception:  # noqa: BLE001 — 观测故障不得影响业务
        span_tid = None
    return span_tid or _turn_trace_id.get() or _ZERO_TRACE_ID


def traceparent_header() -> dict[str, str]:
    """出站请求的 W3C traceparent 头。

    格式 `00-<trace-id>-<span-id>-01`;span-id 每次请求随机生成(W3C 禁止
    parent-id 全零——全零会让严格解析端丢弃整个头),后端 MDC
    消费只取 trace-id 段(按 `-` 分段第 2 段),真实 span 关联等接入分布式
    追踪再补。
    """
    import secrets

    return {"traceparent": f"00-{current_trace_id()}-{secrets.token_hex(8)}-01"}


def _active_span_trace_id() -> str | None:
    """当前 OTel 上下文活跃 span 的 32-hex trace id;无 span 返回 None。

    NOTE: 与 langfuse.get_current_trace_id() 读同一个 OTel 上下文(Langfuse v4
    即 OTel 架构),绕开 get_client()——未配置凭证时后者每次调用都打 auth ERROR 日志。
    """
    from opentelemetry import trace

    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def langfuse_callbacks() -> list[BaseCallbackHandler]:
    """返回应挂到 LangGraph invoke 的 callback 列表;不可用时为空列表。

    全局 Langfuse client 只初始化一次(SDK v3 单例);handler 无参构造,
    从全局 client 取凭证。
    """
    global _warned_no_config
    settings = get_settings()
    missing = not (
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    )
    if missing:
        if not _warned_no_config:
            logger.warning("Langfuse 未配置(host/public_key/secret_key),本进程不上报 trace")
            _warned_no_config = True
        return []
    try:
        handler = _build_handler(settings)
    except Exception:  # noqa: BLE001 — fail-open:观测失败绝不拖垮主流程
        logger.warning("Langfuse handler 构造失败,trace 上报已停用", exc_info=True)
        return []
    return [handler]


def _build_handler(settings: Any) -> Any:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    return _PiiMaskedLangfuseHandler(CallbackHandler())


class _PiiMaskedLangfuseHandler:
    """进 Langfuse 前对 prompt/消息文本统一脱敏的 handler 包装。

    checkpointer 仍保留对话原文——它既是会话连续性的前提,也是保留期清理的
    作用对象;trace 面不再出现可识别原文(手机/身份证/邮箱/QQ/学号,规则见
    security/pii.py)。包装而非继承:langfuse CallbackHandler 随 SDK
    版本演进,只覆写消息入口两个方法,其余原样委托。

    注意必须**拷贝**消息对象再改 content——callback 拿到的是图状态里
    同一批对象,就地改会污染真实对话历史。

    fail-closed 契约:隐私过滤是**不可 fail-open** 的一环——
    「拷贝失败/遍历过深/形状不认识」就照常上报等于把原文送进 trace。
    与 ADR-0005(观测失败不拖垮主链路)不冲突:这里的失败只让**该条**
    上报降级为定值载荷,主链路照常运行。三条规则:
      1. 掩码后无法安全构造副本 → 丢弃该条(返回 None,或整体跳过上报);
      2. 遍历超深(>MAX_DEPTH)→ 返回定值占位串,不回传原值;
      3. 形状不认识(Pydantic model/LLMResult generations 等)→ 转成
         受控最小投影,不原样委托。
    """

    #: 递归遍历深度上限,超过即视为不可安全脱敏(返回占位符而非原值)
    _MAX_DEPTH = 6
    #: 无法脱敏时的固定替换载荷(不泄漏任何原值)
    _REDACTED = "[REDACTED]"

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _masked_copy(self, message: Any) -> Any:
        """按消息构造脱敏副本;无法安全脱敏时返回 None(调用方丢弃该条)。

        空/无 content 的消息本身无 PII,原样放行(copy 失败也不影响)。
        """
        from official_agent.security.pii import mask_pii

        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content:
            return message
        masked = mask_pii(content)
        if masked == content:
            return message  # 无 PII,原对象可安全上报
        try:
            return message.model_copy(update={"content": masked})
        except Exception:  # noqa: BLE001 — 改不动就不能上报原文(fail-closed)
            logger.warning(
                "Langfuse PII:消息副本构造失败,该条不上报(fail-closed)",
                exc_info=True,
            )
            return None

    def _mask_payload(self, value: Any, depth: int = 0) -> Any:
        """递归掩 trace 载荷:chain_start/end 的 inputs/outputs
        会携带原始 messages 与图状态,只掩 chat_model/llm 入口挡不住。

        深度超限与未知形状一律降级为定值,绝不回传原值。
        """
        from official_agent.security.pii import mask_pii

        if depth > self._MAX_DEPTH:
            # 深层结构无法保证脱敏彻底 → 定值占位,不带任何原值
            return self._REDACTED
        if isinstance(value, str):
            return mask_pii(value)
        if isinstance(value, dict):
            return {k: self._mask_payload(v, depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            masked = [self._mask_payload(v, depth + 1) for v in value]
            return type(value)(masked) if isinstance(value, tuple) else masked
        content = getattr(value, "content", None)
        if isinstance(content, str) and content:
            masked = mask_pii(content)
            if masked == content:
                return value  # 无 PII
            try:
                return value.model_copy(update={"content": masked})
            except Exception:  # noqa: BLE001 — 副本构造失败 → 丢弃该条(fail-closed)
                logger.warning(
                    "Langfuse PII:载荷副本构造失败,该条降级为占位(fail-closed)",
                    exc_info=True,
                )
                return self._REDACTED
        return self._project_unknown(value, depth)

    def _project_unknown(self, value: Any, depth: int) -> Any:
        """未知形状(pydantic model / LLMResult generations / dataclass 等)
        的受控最小投影:原样委托会让嵌套 generations 里的
        原文绕过掩码。只保留可安全脱敏的文本字段,其余降级为占位。
        """
        generations = getattr(value, "generations", None)
        if generations is not None:
            return self._REDACTED
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return self._mask_payload(model_dump(), depth + 1)
            except Exception:  # noqa: BLE001 — 投影失败 → 占位(fail-closed)
                return self._REDACTED
        return value

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> Any:
        # 构造不出脱敏副本的消息(_masked_copy 返回 None)整条丢弃,
        # 绝不回落原对象。全部被丢弃时跳过本次上报,把原文彻底挡在 trace 外。
        masked = []
        for batch in messages:
            kept = [c for m in batch if (c := self._masked_copy(m)) is not None]
            masked.append(kept)
        if not any(masked):
            logger.warning("Langfuse PII:本次 prompt 全部无法安全脱敏,跳过上报(fail-closed)")
            return None
        return self._inner.on_chat_model_start(serialized, masked, **kwargs)

    def on_chain_start(self, serialized: Any, inputs: Any, **kwargs: Any) -> Any:
        masked = self._mask_payload(inputs)
        return self._inner.on_chain_start(serialized, masked, **kwargs)

    def on_chain_end(self, outputs: Any, **kwargs: Any) -> Any:
        masked = self._mask_payload(outputs)
        return self._inner.on_chain_end(masked, **kwargs)

    def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> Any:
        return self._inner.on_tool_start(serialized, self._mask_payload(input_str), **kwargs)

    def on_tool_end(self, output: Any, **kwargs: Any) -> Any:
        # 工具返回是 trace 原文的最大来源(search_resumes 等
        # 工具未做返回层脱敏),这里兜底掩一层
        return self._inner.on_tool_end(self._mask_payload(output), **kwargs)

    def on_chat_model_end(self, response: Any, **kwargs: Any) -> Any:
        return self._inner.on_chat_model_end(self._mask_payload(response), **kwargs)

    def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> Any:
        from official_agent.security.pii import mask_pii

        return self._inner.on_llm_start(serialized, [mask_pii(p) for p in prompts], **kwargs)


def eval_job_trace_id(job_id: int) -> str:
    """评测 job 的确定性 correlation id。

    初筛 job 没有对话轮,用 job 身份派生稳定 W3C trace id:同一 job 在
    Langfuse trace、出站 Backend 请求 traceparent、审计 trace_id、结构化
    日志四面同 id,分钟级定位失败阶段(见 docs/eval-observability.md)。
    """
    return to_w3c_trace_id(f"eval-job-{job_id}")
