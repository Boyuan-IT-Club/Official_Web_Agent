"""对话观测落账(web/telemetry.log_conversation)单测:字段透传与取值时机。

落库本身(SQL/PII)在 test_state_conversation.py;这里只看 telemetry 这一层交给
数据层的参数。
"""

import asyncio
import types

from official_agent import observability
from official_agent.web import telemetry


def _session() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        session_id="web:u7:8f3a9c2b", identity={"user_id": 7, "source": "web"}
    )


async def _drain() -> None:
    await asyncio.gather(*list(telemetry._background_tasks))


async def test_log_conversation_carries_turn_trace_id_and_guard_verdict(monkeypatch) -> None:
    written: list[dict] = []
    monkeypatch.setattr(
        "official_agent.state.conversation.write_conversation", lambda **kw: written.append(kw)
    )
    token = observability.set_turn_trace_id("req-123")
    try:
        telemetry.log_conversation(
            _session(),
            user_message="有几份简历?",
            reply_summary="我没有可用的数据查询权限。",
            tools=[],
            duration_ms=5,
            guard_verdict="triggered:查询结果",
        )
    finally:
        # 入口在后台写入跑起来之前就复位了 trace id:落账用的必须是调用那一刻的值
        observability.reset_turn_trace_id(token)
    await _drain()
    (row,) = written
    assert row["trace_id"] == observability.to_w3c_trace_id("req-123")
    assert row["guard_verdict"] == "triggered:查询结果"
    assert row["thread_id"] == "web:u7:8f3a9c2b"


async def test_log_conversation_defaults_guard_verdict_to_none(monkeypatch) -> None:
    written: list[dict] = []
    monkeypatch.setattr(
        "official_agent.state.conversation.write_conversation", lambda **kw: written.append(kw)
    )
    telemetry.log_conversation(
        _session(), user_message="hi", reply_summary="你好", tools=[], duration_ms=1
    )
    await _drain()
    assert written[0]["guard_verdict"] is None
