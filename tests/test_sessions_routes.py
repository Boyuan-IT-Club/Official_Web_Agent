"""会话管理端点测试(M6 #115 G2/G3):用户历史会话/回看,管理员按用户查看。

同 test_web_routes 先例:TestClient + monkeypatch,不真连库/checkpointer。
关键契约:
- /sessions 只列本人会话(agent_threads 属主过滤)
- /sessions/{tid}/messages:resolve_thread 属主硬校验,非属主/已终结 → 404
  (不区分原因,防会话枚举翻看 PII,SEC-07)
- /admin/sessions* 要求 agent:monitor;admin 回看不做属主限制(运营排查),
  已终结会话原文可见(status 标注)
- 原文投影:只保留 user/assistant 文本,工具中间态/空内容跳过
- checkpointer 不可用 → 503(显式数据查询 fail-closed,不给空假象)
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from official_agent.state.threads import ThreadRecord
from official_agent.web.app import create_app


@contextlib.asynccontextmanager
async def _fake_checkpointer() -> AsyncIterator[None]:
    yield None


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


def _identity(user_id: int = 7, monitor: bool = False) -> dict:
    return {
        "user_id": user_id,
        "role": "admin" if monitor else "candidate",
        "role_names": ["管理员" if monitor else "申请人"],
        "permission_codes": ["agent:monitor"] if monitor else ["candidate:read:own"],
        "source": "web",
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _install_resolve(monkeypatch: pytest.MonkeyPatch, identity: dict) -> None:
    from official_agent.web import routes

    async def _resolve(*_a: object, **_k: object) -> dict:
        return identity

    monkeypatch.setattr(routes, "resolve", _resolve)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer tok"}


class _FakeCheckpointer:
    """aget_state 返回预置消息;记录 thread_id 供断言。"""

    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.seen_thread_ids: list[str] = []

    async def aget_state(self, config):
        self.seen_thread_ids.append(config["configurable"]["thread_id"])
        return SimpleNamespace(values={"messages": self._messages})


# ── 用户侧(G2)─────────────────────────────────────────────────────────


def test_sessions_lists_own_threads_sorted_by_activity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import conversation, threads

    _install_resolve(monkeypatch, _identity(user_id=7))
    monkeypatch.setattr(
        threads,
        "list_active_threads",
        lambda owner=None: (
            [_thread("web:u7:aaa", 7), _thread("web:u7:bbb", 7)]
            if owner == 7
            else [_thread("web:u8:zzz", 8)]
        ),
    )
    monkeypatch.setattr(
        conversation,
        "session_overview",
        lambda tids: {
            "web:u7:bbb": {"rounds": 3, "last_at": "2026-09-06T10:00:00Z", "preview": "最新问题"},
        },
    )

    resp = client.get("/api/agent/sessions", headers=_auth_headers())
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["thread_id"] for i in items] == ["web:u7:bbb", "web:u7:aaa"]  # 活跃度排序
    assert items[0]["rounds"] == 3 and items[0]["preview"] == "最新问题"
    assert items[1]["rounds"] == 0  # 无落行的会话也在列表(agent_threads 为准)


def test_session_messages_owner_reads_transcript(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import threads

    _install_resolve(monkeypatch, _identity(user_id=7))
    monkeypatch.setattr(
        threads, "resolve_thread", lambda tid, uid: _thread(tid, uid) if uid == 7 else None
    )
    fake_cp = _FakeCheckpointer(
        [
            HumanMessage(content="我的面试是什么时候"),
            AIMessage(content="9月12日 09:00"),
            ToolMessage(content="工具结果不进回看", tool_call_id="c1"),
            HumanMessage(content=""),
        ]
    )
    client.app.state.checkpointer = fake_cp

    resp = client.get("/api/agent/sessions/web:u7:aaa/messages", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["messages"] == [
        {"role": "user", "content": "我的面试是什么时候"},
        {"role": "assistant", "content": "9月12日 09:00"},
    ]
    assert fake_cp.seen_thread_ids == ["web:u7:aaa"]


def test_session_messages_non_owner_or_missing_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import threads

    _install_resolve(monkeypatch, _identity(user_id=7))
    monkeypatch.setattr(threads, "resolve_thread", lambda tid, uid: None)  # 非属主/终结
    client.app.state.checkpointer = _FakeCheckpointer([])

    resp = client.get("/api/agent/sessions/web:u8:zzz/messages", headers=_auth_headers())
    assert resp.status_code == 404  # 不区分原因,防枚举


def test_session_messages_without_checkpointer_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import threads

    _install_resolve(monkeypatch, _identity(user_id=7))
    monkeypatch.setattr(threads, "resolve_thread", lambda tid, uid: _thread(tid, uid))
    client.app.state.checkpointer = None  # PG 不可用

    resp = client.get("/api/agent/sessions/web:u7:aaa/messages", headers=_auth_headers())
    assert resp.status_code == 503


def test_sessions_requires_auth(client: TestClient) -> None:
    assert client.get("/api/agent/sessions").status_code == 401


# ── 管理员侧(G3)─────────────────────────────────────────────────────────


def test_admin_sessions_requires_monitor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_resolve(monkeypatch, _identity(user_id=8, monitor=False))
    resp = client.get("/api/agent/admin/sessions", headers=_auth_headers())
    assert resp.status_code == 403


def test_admin_sessions_lists_and_filters_by_user(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import conversation, threads

    _install_resolve(monkeypatch, _identity(user_id=1, monitor=True))
    seen: dict = {}
    monkeypatch.setattr(
        threads,
        "list_active_threads",
        lambda owner=None: seen.update({"owner": owner}) or [_thread("web:u7:aaa", 7)],
    )
    monkeypatch.setattr(
        conversation,
        "session_overview",
        lambda tids: {
            "web:u7:aaa": {
                "rounds": 2,
                "last_at": "2026-09-06T09:00:00Z",
                "preview": "你好",
            }
        },
    )

    resp = client.get("/api/agent/admin/sessions?user_id=7", headers=_auth_headers())
    assert resp.status_code == 200
    assert seen["owner"] == 7
    items = resp.json()["items"]
    assert items[0]["owner_user_id"] == 7
    assert items[0]["rounds"] == 2


def test_admin_session_messages_any_owner_with_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import threads

    _install_resolve(monkeypatch, _identity(user_id=1, monitor=True))
    monkeypatch.setattr(threads, "get_thread", lambda tid: _thread(tid, 8, status="active"))
    client.app.state.checkpointer = _FakeCheckpointer(
        [HumanMessage(content="你好"), AIMessage(content="你好呀")]
    )

    resp = client.get("/api/agent/admin/sessions/web:u8:aaa/messages", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["owner_user_id"] == 8 and data["status"] == "active"
    assert len(data["messages"]) == 2


def test_admin_session_messages_unknown_thread_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import threads

    _install_resolve(monkeypatch, _identity(user_id=1, monitor=True))
    monkeypatch.setattr(threads, "get_thread", lambda tid: None)

    resp = client.get("/api/agent/admin/sessions/web:u1:none/messages", headers=_auth_headers())
    assert resp.status_code == 404
