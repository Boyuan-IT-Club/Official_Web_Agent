"""观测接线(OBS-01):LangGraph → Langfuse,fail-open。

用法(图入口):
    callbacks = langfuse_callbacks()
    result = await graph.ainvoke(state, config={"callbacks": callbacks})

fail-open 语义(ADR-0005):观测挂了不许影响主流程——
- 未配置(缺 host/key)→ 返回 [],打一次 WARNING,不打扰每次调用;
- 构造异常 → 捕获降级为 []。
写路径是 fail-closed,观测是 fail-open,两者方向相反,别混。

红线联动(#68,SEC-08):本接线会把工具返回原文上报 trace。get_resume_detail
返回完整简历(含 PII)——在 #68 拍板「脱敏下沉工具层 vs 采集点二次脱敏」之前,
含 PII 的工具经本 handler 上报即落 Langfuse 库,接入评估流水线前必须先解决。

prompt 版本对比(ADR-0004):prompt 唯一权威是 prompts/ 文件 frontmatter,
Langfuse 只读镜像;同步脚本待 prompt 体系落地后随 GRA 任务补。
"""

import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from boyuan_agent.config import get_settings

logger = logging.getLogger(__name__)

_warned_no_config = False


def langfuse_callbacks() -> list[BaseCallbackHandler]:
    """返回应挂到 LangGraph invoke 的 callback 列表;不可用时为空列表。

    全局 Langfuse client 只初始化一次(SDK v3 单例);handler 无参构造,
    从全局 client 取凭证。
    """
    global _warned_no_config
    settings = get_settings()
    missing = not (
        settings.langfuse_host
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
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


def _build_handler(settings: Any) -> BaseCallbackHandler:
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    return CallbackHandler()
