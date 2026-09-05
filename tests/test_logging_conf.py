"""M6 #113 日志配置测试:stdout + .log 落盘 + RotatingFileHandler 定量打包。

setup_logging(log_dir, level) 用注入目录(测试不污染真实日志)。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from official_agent.logging_conf import setup_logging


@pytest.fixture(autouse=True)
def _clean_root_handlers():
    """每个测试前清 root handlers——setup_logging 幂等会跳过已配置的
    handler,不清则后续测试的注入 log_dir 不生效。"""
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers[:] = []
    yield
    root.handlers[:] = saved


def test_setup_logging_adds_stream_and_file_handlers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        setup_logging(log_dir=log_dir, level="INFO")
        handlers = logging.getLogger().handlers
        types = [type(h).__name__ for h in handlers]
        assert "StreamHandler" in types  # stdout
        assert "RotatingFileHandler" in types  # 落盘 + 定量打包
        # .log 文件在注入目录生成
        assert any(log_dir.glob("*.log"))


def test_setup_logging_is_idempotent() -> None:
    """重复调用不重复加 handler(防 app 重载叠加)。"""
    with tempfile.TemporaryDirectory() as tmp:
        setup_logging(log_dir=Path(tmp), level="INFO")
        count_after_first = len(logging.getLogger().handlers)
        setup_logging(log_dir=Path(tmp), level="INFO")
        count_after_second = len(logging.getLogger().handlers)
    assert count_after_first == count_after_second


def test_logger_writes_to_file() -> None:
    """真实 log 落盘可读(非仅 stdout)。"""
    with tempfile.TemporaryDirectory() as tmp:
        log_dir = Path(tmp)
        setup_logging(log_dir=log_dir, level="INFO")
        logging.getLogger("official_agent.test").info("hello-m6-log")
        # handler flush
        for h in logging.getLogger().handlers:
            h.flush()
        log_files = list(log_dir.glob("*.log"))
        assert log_files, "无 .log 文件生成"
        content = log_files[0].read_text(encoding="utf-8")
        assert "hello-m6-log" in content