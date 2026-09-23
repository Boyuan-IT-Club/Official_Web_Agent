"""trace 串联测试末段:日志四律(request_id 贯穿)的两个验收用例。"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from official_agent.logging_conf import setup_logging
from official_agent.observability import reset_turn_trace_id, set_turn_trace_id
from official_agent.web.app import create_app


def test_middleware_assigns_request_trace_id() -> None:
    """入口中间件:响应头 X-Request-Id(32-hex)。

    四律验收:同一 id 串起 web 入口 → 下游日志;入站 X-Request-Id 优先,
    供网关/上游串联。"""
    with TestClient(create_app()) as client:
        resp = client.get("/health")
        resp2 = client.get("/health", headers={"X-Request-Id": "my-upstream-correlation-id"})
    rid = resp.headers.get("x-request-id")
    assert rid and re.fullmatch(r"[0-9a-f]{32}", rid), "响应头必须带归一 32-hex trace id"
    rid2 = resp2.headers.get("x-request-id")
    assert rid2 == hashlib.sha256(b"my-upstream-correlation-id").hexdigest()[:32], (
        "入站 id 确定性归一(同值同像,跨端可复算)"
    )


def test_log_records_carry_trace_id() -> None:
    """日志四律:每条记录自带当前 trace id(过滤器注入,永非空)。

    走真实链路断言:setup_logging → 置轮 id → 落盘行含 `[<归一 id>]`。"""
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers[:] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            setup_logging(log_dir=Path(tmp), level="INFO")
            token = set_turn_trace_id("web:u7:abc1234")
            try:
                logging.getLogger("official_agent.test.trace").warning("串联探针")
            finally:
                reset_turn_trace_id(token)
            for h in root.handlers:
                h.flush()
            log_files = list(Path(tmp).glob("*.log"))
            assert log_files, "无落盘日志"
            content = log_files[0].read_text(encoding="utf-8")
            want = hashlib.sha256(b"web:u7:abc1234").hexdigest()[:32]
            assert f"[{want}]" in content, "日志行必须携带当前 trace id"
    finally:
        root.handlers[:] = saved
