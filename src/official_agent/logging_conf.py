"""日志配置:stdout + .log 落盘 + 定量打包。

现状:repo 无任何 logging 配置(basicConfig/dictConfig 均无),部署后日志
全靠 Python 默认(stderr)。本模块提供 setup_logging():
- StreamHandler → stdout(容器日志)
- RotatingFileHandler → {log_dir}/official-agent.log(落盘,运维可查)
- 定量打包:maxBytes=10MB,backupCount=5(RotatingFileHandler)
- 幂等:重复调用不叠加 handler(app reload/多次 setup 安全)
"""

from __future__ import annotations

import contextlib
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# trace_id 由 TraceIdFilter 注入(observability.current_trace_id,永非空):
# 一次请求从 web 入口到 tools/state 的所有日志共享同一 id,按 id 串成一条线。
_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(trace_id)s] %(message)s"
_LOG_FILENAME = "official-agent.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_BACKUP_COUNT = 5


class TraceIdFilter(logging.Filter):
    """给每条日志记录注入当前 trace id(排障串联键;详见 observability)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        from official_agent.observability import current_trace_id

        record.trace_id = current_trace_id()
        return True


def setup_logging(log_dir: Path | None = None, level: str = "INFO") -> None:
    """配置 root logger:stdout + RotatingFileHandler(幂等)。

    log_dir 默认取 ~/.official-agent/logs(与凭证目录同族);测试注入临时目录。
    幂等:检查我们自己的 handler(属性标记),不误伤应用/依赖加的 handler。
    日志目录/文件权限收紧(0700/0600,同 credentials.py 先例)——日志含
    PII 脱敏后内容与用量,多用户主机不可让其他本地用户可读。
    """
    root = logging.getLogger()
    # 幂等:已加过我们标记的 file handler 则不重复加
    if any(getattr(h, "_official_agent_handler", False) for h in root.handlers):
        return

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(_FORMAT)
    trace_filter = TraceIdFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(trace_filter)
    root.addHandler(stream)

    if log_dir is None:
        from official_agent.config import get_settings

        log_dir = Path(get_settings().credentials_dir).expanduser() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_handler = RotatingFileHandler(
        log_dir / _LOG_FILENAME,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    # 标记:幂等检查用(见上方 guard),避免误伤应用加的 RotatingFileHandler
    file_handler._official_agent_handler = True  # type: ignore[attr-defined]
    file_handler.addFilter(trace_filter)
    # 权限收紧:日志文件 0600(默认 umask 可能 0644)
    with contextlib.suppress(OSError):
        (log_dir / _LOG_FILENAME).chmod(0o600)
    root.addHandler(file_handler)
