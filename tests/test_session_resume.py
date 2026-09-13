"""#169 重启续聊 + 有界 session registry 路由测试。

同 test_web_routes 先例:TestClient + monkeypatch,不真连库/checkpointer。
关键契约:
- 内存未命中 + 显式 session_id → resolve_thread 属主/active 校验;
  通过则以原 thread_id 重建运行时(is_new=False,不再静默换新会话);
- 跨属主/已终结/不存在统一 404(不区分原因,SEC-07 防枚举);
- resolve_thread 故障 → 503 fail-closed;
- 注册表有界:空闲 TTL 与容量上限淘汰只删运行时对象,不碰 PG。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from official_agent.state.threads import ThreadRecord
from official_agent.web import routes
from official_agent.web.app import create_app


class _CheckpointerStub:
    """checkpointer 替身(#194):恢复路径现在要求非 None,测试不能再给 None。"""


@contextlib.asynccontextmanager
async def _fake_checkpointer() -> AsyncIterator[object]:
    yield _CheckpointerStub()


def _thread(tid: str, owner: int = 7, status: str = "active") -> ThreadRecord:
    return ThreadRecord(
        thread_id=tid,
        owner_user_id=owner,
        channel="web",
        status=status,
        subject=None,
        created_at=None,
        deleted_at=None,
    )


@pytest.fixture(autouse=True)
def _reset_registry():
    routes._sessions.clear()
    routes._sessions_last_access.clear()
    yield
    routes._sessions.clear()
    routes._sessions_last_access.clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: object())
    with TestClient(create_app()) as c:
        yield c


def _identity(user_id: int = 7) -> dict:
    return {
        "user_id": user_id,
        "role": "candidate",
        "role_names": ["申请人"],
        "permission_codes": ["candidate:read:own"],
        "source": "web",
    }


def _install(monkeypatch: pytest.MonkeyPatch, *, thread: ThreadRecord | None, user_id: int = 7):
    from official_agent.config import get_settings

    async def _resolve(*_a: object, **_k: object) -> dict:
        return _identity(user_id)

    monkeypatch.setattr(routes, "resolve", _resolve)
    monkeypatch.setattr(
        routes,
        "resolve_thread",
        lambda tid, uid: thread,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "session_registry_max", 500)
    monkeypatch.setattr(settings, "session_registry_ttl_seconds", 3600)


def _sse_events(resp) -> list[dict]:
    body = "".join(resp.iter_text())
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data: ")]


def test_resume_after_restart_reuses_same_thread(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启(内存空)后携旧 session_id:原 thread 恢复,session_id 不变,is_new=False。"""
    _install(monkeypatch, thread=_thread("web:u7:abcd1234"))
    created: list = []
    monkeypatch.setattr(
        routes,
        "create_thread",
        lambda *a, **k: (
            created.append(1) or (_ for _ in ()).throw(AssertionError("恢复路径不得新建 thread"))
        ),
    )

    resp = client.post(
        "/api/agent/chat",
        json={"message": "继续", "session_id": "web:u7:abcd1234"},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    assert created == [], "恢复路径不得新建 thread"
    assert "web:u7:abcd1234" in routes._sessions
    events = _sse_events(resp)
    session_event = next(e for e in events if e.get("type") == "session" or "session" in e)
    assert session_event.get("session_id") == "web:u7:abcd1234"
    assert session_event.get("created") is False


def test_resume_rejects_cross_owner_terminated_unknown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跨属主/已终结/不存在(resolve_thread None)统一 404,不区分原因。"""
    _install(monkeypatch, thread=None)
    resp = client.post(
        "/api/agent/chat",
        json={"message": "继续", "session_id": "web:u7:ghost01"},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 404
    assert "无权访问" in resp.json()["detail"] or "不存在" in resp.json()["detail"]


def test_resume_fail_closed_when_registry_check_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve_thread 故障 → 503,绝不静默新建会话顶替。"""

    def _boom(tid, uid):
        raise RuntimeError("pg down")

    _install(monkeypatch, thread=None)
    monkeypatch.setattr(routes, "resolve_thread", _boom)
    resp = client.post(
        "/api/agent/chat",
        json={"message": "继续", "session_id": "web:u7:abcd1234"},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 503
    assert "web:u7:abcd1234" not in routes._sessions, "校验失败不得把新会话顶替建档"


def _state_stub(session_id: str) -> routes._SessionState:
    """_SessionState 替身:淘汰逻辑现在读 turn_lock(#194),不能拿 object() 充数。"""
    return routes._SessionState(session_id, _identity(), "tok", object())


def test_registry_ttls_idle_and_cap(monkeypatch) -> None:
    import time as _time

    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_registry_max", 3)
    monkeypatch.setattr(settings, "session_registry_ttl_seconds", 60)

    now = _time.monotonic()
    for i in range(5):
        sid = f"web:u7:s{i}"
        routes._sessions[sid] = _state_stub(sid)  # 运行时对象替身
        routes._sessions_last_access[sid] = now - (100 if i < 2 else 0)
    routes._evict_sessions_locked(now)
    # s0/s1 空闲超 TTL 被淘汰
    assert "web:u7:s0" not in routes._sessions
    assert "web:u7:s1" not in routes._sessions
    # 容量 3:剩 3 条(s2/s3/s4)
    assert len(routes._sessions) == 3
    assert set(routes._sessions) == {"web:u7:s2", "web:u7:s3", "web:u7:s4"}


def test_registry_lru_touch_keeps_recent(monkeypatch) -> None:
    """LRU:被访问过的会话不应比更晚插入但未访问的先淘汰。"""
    import time as _time

    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_registry_max", 2)
    monkeypatch.setattr(settings, "session_registry_ttl_seconds", 3600)

    now = _time.monotonic()
    for sid in ("web:u7:old", "web:u7:new"):
        routes._sessions[sid] = _state_stub(sid)
        routes._sessions_last_access[sid] = now
    # 访问 old(移到 LRU 尾)
    routes._sessions.move_to_end("web:u7:old")
    routes._sessions_last_access["web:u7:old"] = now + 1
    # 插入第三条 → 超 2 容量 → LRU 头(new 未被访问)被淘汰
    routes._sessions["web:u7:third"] = _state_stub("web:u7:third")
    routes._sessions_last_access["web:u7:third"] = now + 2
    routes._evict_sessions_locked(now + 2)
    assert "web:u7:old" in routes._sessions
    assert "web:u7:third" in routes._sessions
    assert "web:u7:new" not in routes._sessions


# ── 真 PG 档案层:resolve_thread 属主/状态往返(#169 验收) ──


def _pg_url() -> str:
    import os

    return os.environ.get("POSTGRES_URL", "")


def _pg_available() -> bool:
    url = _pg_url()
    if not url:
        return False
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001 — 连不上即 skip
        return False


def test_resolve_thread_roundtrip_real_pg() -> None:
    """真 PG:建档 → 属主 resolve 命中 / 跨属主与软删后 resolve 为 None。"""
    import os
    import uuid

    import psycopg
    import pytest

    from official_agent.config import get_settings
    from official_agent.state import threads as thread_store

    url = _pg_url()
    if not url:
        pytest.skip("需要真 PostgreSQL(POSTGRES_URL)")
    owner = uuid.uuid4().int % 1_000_000 + 10
    stranger = owner + 1
    original = get_settings().postgres_url
    get_settings().postgres_url = url
    try:
        rec = thread_store.create_thread("web", owner, subject="web-chat")
        tid = rec.thread_id
        hit = thread_store.resolve_thread(tid, owner)
        assert hit is not None and hit.thread_id == tid
        assert thread_store.resolve_thread(tid, stranger) is None, "跨属主必须拒"
        thread_store.soft_delete_thread(tid, owner_user_id=owner)
        assert thread_store.resolve_thread(tid, owner) is None, "已终结必须拒"
        with psycopg.connect(url) as conn:
            conn.execute("DELETE FROM agent_threads WHERE thread_id = %s", (tid,))
    finally:
        get_settings().postgres_url = original
    _ = os  # 保持导入完整


# ── #170:单轮墙钟超时 / 安全错误契约 / 递归上限 / 全局并发闸 ──


class _HangingAgent:
    """astream 永不产出的假 agent:触发墙钟超时。"""

    def __init__(self) -> None:
        self.config_seen: RunnableConfig | None = None

    async def astream(self, *args, config: RunnableConfig, **kwargs):
        self.config_seen = config
        import asyncio as _aio

        await _aio.sleep(60)
        yield "updates", {"agent": {"messages": []}}

    async def aget_state(self, config: RunnableConfig):
        return None


class _BoomAgent:
    """astream 立刻抛内部异常:断言原始异常不外泄。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def astream(self, *args, config: RunnableConfig, **kwargs):
        raise self._exc
        yield  # pragma: no cover

    async def aget_state(self, config: RunnableConfig):
        return None


def _install_turn_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "turn_wall_clock_timeout", 1)
    monkeypatch.setattr(settings, "turn_recursion_limit", 25)
    monkeypatch.setattr(settings, "model_call_global_concurrency", 4)
    monkeypatch.setattr(settings, "model_gate_acquire_timeout", 2)


def test_turn_timeout_returns_stable_copy_and_releases_lock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#170:卡住的轮次在墙钟时限内中止,客户端收稳定文案,锁可靠释放。"""
    import time as _time

    agent = _HangingAgent()
    _install(monkeypatch, thread=None)
    _install_turn_settings(monkeypatch)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: agent)
    t0 = _time.monotonic()
    resp = client.post(
        "/api/agent/chat",
        json={"message": "你好"},
        headers={"Authorization": "Bearer tok"},
    )
    elapsed = _time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed < 30, "超时必须在墙钟时限内生效而非挂死"
    events = _sse_events(resp)
    err = [e for e in events if e.get("type") == "error"]
    assert err and err[0]["code"] == "timeout"
    assert "超时" in err[0]["message"]
    # 锁已释放:同会话立刻再聊不再回 busy
    resp2 = client.post(
        "/api/agent/chat",
        json={"message": "还在吗", "session_id": None},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp2.status_code == 200
    assert not [e for e in _sse_events(resp2) if e.get("code") == "busy"]


def test_error_events_never_leak_internal_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#170:原始异常(内网地址/SDK 体)不得进 SSE,只给稳定文案+trace。"""
    agent = _BoomAgent(RuntimeError("SECRET postgres://user:pw@10.0.0.9:5432/db boomed"))
    _install(monkeypatch, thread=None)
    _install_turn_settings(monkeypatch)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: agent)
    resp = client.post(
        "/api/agent/chat",
        json={"message": "你好"},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    err = [e for e in _sse_events(resp) if e.get("type") == "error"]
    assert err and err[0]["code"] == "unknown"
    assert "SECRET" not in err[0]["message"]
    assert "postgres://" not in err[0]["message"]
    assert "trace:" in err[0]["message"]


def test_recursion_limit_forwarded_in_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#170:recursion_limit 显式进 astream config。"""
    agent = _HangingAgent()
    _install(monkeypatch, thread=None)
    _install_turn_settings(monkeypatch)
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: agent)
    # 用立即成功的 agent 而非挂起者:换 _FakeAgent 风格
    from langchain_core.messages import AIMessage

    class _OkAgent:
        async def astream(self, *args, config: RunnableConfig, **kwargs):
            self.config_seen = config
            yield "messages", (AIMessage(content="好"), {})
            yield "updates", {"agent": {"messages": []}}

        async def aget_state(self, config: RunnableConfig):
            return None

    ok = _OkAgent()
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: ok)
    client.post(
        "/api/agent/chat",
        json={"message": "你好"},
        headers={"Authorization": "Bearer tok"},
    )
    assert ok.config_seen is not None
    assert ok.config_seen.get("recursion_limit") == 25


def test_model_gate_saturation_returns_busy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#170:全局并发闸满 → 稳定 busy 事件,不无限排队占 SSE。"""

    class _FullGate:
        """永不放行的假闸(闸满的最坏情形)。"""

        async def acquire(self) -> None:
            import asyncio as _aio

            await _aio.sleep(60)

    _install(monkeypatch, thread=None)
    _install_turn_settings(monkeypatch)
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "model_gate_acquire_timeout", 1)
    monkeypatch.setattr(routes, "_get_model_gate", lambda: _FullGate())
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: _FakeGateAgent())
    resp = client.post(
        "/api/agent/chat",
        json={"message": "你好"},
        headers={"Authorization": "Bearer tok"},
    )
    err = [e for e in _sse_events(resp) if e.get("type") == "error"]
    assert err and err[0]["code"] == "busy"


class _FakeGateAgent:
    async def astream(self, *args, config: RunnableConfig, **kwargs):
        yield "messages", (AIMessage(content="好"), {})

    async def aget_state(self, config: RunnableConfig):
        return None


def test_auth_backend_down_is_503_not_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#170:后端不可达 → 503;凭证无效仍 401——两类失败不再混淆。"""
    from official_agent.tools.client import BackendError, BackendUnavailableError

    async def _down(*_a: object, **_k: object):
        raise BackendUnavailableError("后端连接失败(ConnectError)")

    async def _bad_token(*_a: object, **_k: object):
        raise BackendError("用户令牌无效或已过期,需用户重新登录后重试")

    monkeypatch.setattr(routes, "resolve", _down)
    resp = client.post(
        "/api/agent/chat", json={"message": "你好"}, headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 503

    monkeypatch.setattr(routes, "resolve", _bad_token)
    resp = client.post(
        "/api/agent/chat", json={"message": "你好"}, headers={"Authorization": "Bearer tok"}
    )
    assert resp.status_code == 401


# ── #194 复审 P0/P1 回归 ──


def test_evict_skips_inflight_turn(monkeypatch) -> None:
    """#194 复审 P0:持锁会话不得被 TTL/LRU 淘汰。

    淘汰执行中的运行时会让同一 session_id 的下个请求重建出「新对象 +
    新锁」,与原对象并发写同一 checkpoint thread。此测试钉住:在途会话
    既不被 TTL 清掉,也不被容量挤出。
    """
    import time as _time

    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_registry_max", 1)
    monkeypatch.setattr(settings, "session_registry_ttl_seconds", 60)

    now = _time.monotonic()
    busy = _state_stub("web:u7:busy")
    busy.turn_lock = _HeldLock()  # type: ignore[assignment]
    routes._sessions["web:u7:busy"] = busy
    routes._sessions_last_access["web:u7:busy"] = now - 1000  # 早已过 TTL

    routes._evict_sessions_locked(now)

    assert "web:u7:busy" in routes._sessions, "在途会话被淘汰会让两个 agent 并发写同一 thread"
    assert routes._sessions["web:u7:busy"] is busy


def test_evict_skips_inflight_but_still_evicts_idle(monkeypatch) -> None:
    """#194 复审 P0:在途条目跳过不得阻断其余条目淘汰(容量仍能收敛)。"""
    import time as _time

    from official_agent.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_registry_max", 2)
    monkeypatch.setattr(settings, "session_registry_ttl_seconds", 3600)

    now = _time.monotonic()
    busy = _state_stub("web:u7:busy")
    busy.turn_lock = _HeldLock()  # type: ignore[assignment]
    routes._sessions["web:u7:busy"] = busy
    routes._sessions_last_access["web:u7:busy"] = now
    for sid in ("web:u7:a", "web:u7:b"):
        routes._sessions[sid] = _state_stub(sid)
        routes._sessions_last_access[sid] = now

    routes._evict_sessions_locked(now)

    assert "web:u7:busy" in routes._sessions
    assert len(routes._sessions) <= 2, "空闲条目应被淘汰到容量内"


def test_resume_without_checkpointer_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """#194 复审 P1:档案在但 checkpointer 不可用 → 503,不假装续聊成功。

    旧行为返回 is_new=False 并构建无持久化 agent:用户看到「续聊成功」
    而上下文为空。直接驱动 _get_or_create_session,把 app.state.checkpointer
    置 None 复现该状态(TestClient 生命周期内改不到 app.state)。
    """
    import asyncio as _asyncio

    _install(monkeypatch, thread=_thread("web:u7:abcd1234"))
    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: object())

    class _Req:
        class app:  # noqa: N801 — 复刻 Starlette request.app.state 形状
            class state:  # noqa: N801
                checkpointer = None

    async def _run() -> None:
        await routes._get_or_create_session(
            _Req(), _identity(), "tok", "web:u7:abcd1234"  # type: ignore[arg-type]
        )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _asyncio.run(_run())
    assert exc.value.status_code == 503
    assert "web:u7:abcd1234" not in routes._sessions, "不得构建无 checkpointer 的恢复会话"


def test_gate_busy_logs_conversation_and_emits_one_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#194 复审 P1:闸满必须落 conversation_log,且只发一个 error 事件。

    旧行为在获取闸超时后直接 return,跳过 _log_conversation——资源饱和
    事件恰好在观测面消失。
    """

    _install(monkeypatch, thread=None)
    logged: list[dict] = []
    monkeypatch.setattr(routes, "_log_conversation", lambda session, **kw: logged.append(kw))

    async def _noop_stream(*_a: object, **_k: object):
        raise AssertionError("闸满不得进入 astream")
        yield  # pragma: no cover — 使其成为 async generator

    monkeypatch.setattr(routes, "build_assistant_agent", lambda *a, **k: _FakeAgent(_noop_stream))

    class _Saturated:
        async def acquire(self) -> None:
            raise TimeoutError

        def release(self) -> None:  # pragma: no cover — 未获取则不释放
            raise AssertionError("未获取的闸不得 release")

    monkeypatch.setattr(routes, "_get_model_gate", lambda: _Saturated())

    resp = client.post(
        "/api/agent/chat",
        json={"message": "你好"},
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    errors = [e for e in _sse_events(resp) if e.get("type") == "error"]
    assert len(errors) == 1, "闸满只能发一个 error 事件"
    assert errors[0]["code"] == "busy"
    assert len(logged) == 1, "闸满必须落一行 conversation_log(观测收尾点唯一)"
    assert logged[0]["error_code"] == "busy"


class _HeldLock:
    """已持有的锁替身:locked() 恒 True,acquire/release 不应被调用。"""

    def locked(self) -> bool:
        return True


class _FakeAgent:
    def __init__(self, stream) -> None:
        self._stream = stream

    def astream(self, *_a: object, **_k: object):
        return self._stream()
