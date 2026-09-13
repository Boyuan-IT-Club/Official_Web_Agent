"""PII 出口契约测试(#164,SEC-08/#68):规则表负例/键级掩/输出守卫/审计掩/TTL。"""

import json
from datetime import UTC

import pytest

from official_agent.security.pii import (
    GUARD_NAME_PII_OUTPUT,
    mask_pii,
    mask_pii_deep,
    mask_pii_output,
)

# ── 规则表扩展(#164):邮箱/键级姓名 ──


def test_email_masked() -> None:
    masked = mask_pii("联系 zhangsan@qq.com 或写邮件")
    assert "zhangsan@qq.com" not in masked
    assert "[邮箱]" in masked


def test_name_key_masked_in_deep() -> None:
    payload = {
        "name": "张三",
        "real_name": "李四",
        "major": "计算机科学",
        "nested": {"name": "王五"},
    }
    out = mask_pii_deep(payload)
    assert out["name"] == "〔姓名〕"
    assert out["real_name"] == "〔姓名〕"
    assert out["nested"]["name"] == "〔姓名〕"
    assert out["major"] == "计算机科学"  # 非姓名键不掩


# ── 负例基线(#164):年份/日期不误掩 ──
# #176:12-17 位纯数字(学号/准考证号)改为必须掩(出向模型契约),
# 纯数字长单号因此一并被掩——出向脱敏宁滥勿漏,这是有意取舍。


def test_negative_cases_not_masked() -> None:
    for text in (
        "2024 年毕业",
        "2023-09-01 入学,2026-06 毕业",
        "成绩 3.9/4.0,排名前 10%",
    ):
        assert mask_pii(text) == text, text


def test_positive_cases_still_masked() -> None:
    masked = mask_pii("手机 13812345678,证号 310110200001011234,QQ 123456789")
    assert "13812345678" not in masked
    assert "310110200001011234" not in masked
    assert "123456789" not in masked


# ── 输出守卫(#159 契约「回复出口」;检出→掩码替换照发) ──


def test_output_guard_masks_but_sends() -> None:
    text = "该候选人手机 13812345678,邮箱 zhang@163.com,已进入面试。"
    final, trace = mask_pii_output(text)
    assert "13812345678" not in final
    assert "zhang@163.com" not in final
    assert "已进入面试" in final  # 内容照发,非拦截
    assert trace is not None
    assert trace["guard_name"] == GUARD_NAME_PII_OUTPUT == "pii_output"
    assert trace["verdict"] == "masked"


def test_output_guard_clean_no_trace() -> None:
    text = "该候选人已进入技术部面试环节。"
    final, trace = mask_pii_output(text)
    assert final == text and trace is None


# ── 审计写入口 deep 掩(#164 出口契约) ──


def test_audit_action_masked_before_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.state import audit

    captured: dict = {}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return self

        def execute(self, sql, params=None):
            captured["params"] = params
            return self

        def fetchone(self):
            p = captured["params"]
            return {
                "id": 1,
                "thread_id": "t1",
                "acting_user_id": 7,
                "channel": "web",
                "agent": "eval",
                "action": p[4],
                "decision": p[5],
                "decision_summary": p[6],
                "token": p[7],
                "result": p[8],
                "trace_id": p[9],
                "created_at": "2026-09-10",
            }

    monkeypatch.setattr(audit, "_conn", lambda: _FakeConn())
    record = audit.write_audit(
        thread_id="t1",
        acting_user_id=7,
        channel="web",
        agent="eval",
        action={"resume": "张三 13812345678"},
        decision="u7:approve",
        decision_summary="批准",
        result="ok",
    )
    persisted = record.action if isinstance(record.action, dict) else json.loads(record.action)
    # resume 值是自由文本:手机号文本掩;姓名键级掩只对 name/real_name 键生效
    assert persisted["resume"] == "张三 138****5678"


# ── 确认摘要先掩(#164 §4) ──


def test_require_confirmation_masks_summary(monkeypatch: pytest.MonkeyPatch) -> None:

    captured: dict = {}

    def _fake_interrupt(payload: dict):
        captured.update(payload)
        return "approve"

    monkeypatch.setattr("official_agent.tools.interrupt_guard.interrupt", _fake_interrupt)
    from official_agent.tools.interrupt_guard import require_confirmation

    decision = require_confirmation("将把张三(13812345678)调剂到周六场次")
    assert decision == "approve"
    assert "13812345678" not in captured["summary"]
    assert "****" in captured["summary"]


# ── TTL 清理任务(#164 §4) ──


def test_purge_expired_interrupts_with_fake_conn() -> None:
    import uuid as uuid_mod

    from official_agent.state import pg

    old_id = str(uuid_mod.uuid6()) if hasattr(uuid_mod, "uuid6") else str(uuid_mod.uuid1())
    fresh_id = str(uuid_mod.uuid1())

    class _Cursor:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            if "SELECT DISTINCT thread_id" in sql:
                self.conn.rows = [("old-thread",), ("fresh-thread",)]
            elif "max(checkpoint_id)" in sql:
                tid = params[0]
                self.conn.rows = [(old_id if tid == "old-thread" else fresh_id,)]
            else:
                self.conn.deleted.append((sql.split("FROM")[1].strip().split(" ")[0], params[0]))

        def fetchall(self):
            return self.conn.rows

        def fetchone(self):
            return self.conn.rows[-1] if self.conn.rows else None

    class _FakeConn:
        def __init__(self):
            self.rows = []
            self.deleted = []

        def cursor(self):
            return _Cursor(self)

        def commit(self):
            pass

        def close(self):
            pass

    conn = _FakeConn()
    purged = pg.purge_expired_interrupts(max_age_hours=24, conn=conn)
    # uuid1 均为「现在」, fresh 不删;old 也非 24h 前 → 0(防御语义:宁可不删)
    assert purged == 0
    assert conn.deleted == []


def test_uuid_timestamp_age_parses_and_flags() -> None:
    from datetime import datetime

    from official_agent.state.pg import _uuid_timestamp_age_hours

    # uuid4 可解析但 time 为随机位:垃圾年龄,兜在 age<24 → 不误删;非 UUID 串 → None
    now = datetime(2026, 9, 10, tzinfo=UTC).timestamp()
    import uuid as uuid_mod

    assert _uuid_timestamp_age_hours("not-a-uuid", now) is None
    garbage_age = _uuid_timestamp_age_hours(str(uuid_mod.uuid4()), now)
    assert garbage_age is None or garbage_age < 24  # 不会误删语义


# ── TTL v6 解码与挂起判别(评审 P0/P1 回归) ──


def test_uuid6_age_decodes_rfc9562_layout() -> None:
    """评审 P0 回归:stdlib .time 在 3.12 上按 v1 序解码会得垃圾——实现必须
    显式 v6 重排。以「现在」生成的 uuid6 年龄应 <1h。"""
    import uuid as uuid_mod
    from datetime import datetime

    from official_agent.state.pg import _uuid_timestamp_age_hours

    now = datetime.now(UTC).timestamp()
    if not hasattr(uuid_mod, "uuid6"):  # pragma: no cover - 3.12 生产镜像
        pytest.skip("stdlib uuid6 需 3.14+;3.12 正确性由评审在独立运行时实证")
    u6 = str(uuid_mod.uuid6())
    age = _uuid_timestamp_age_hours(u6, now)
    assert age is not None and -1 < age < 1


def test_uuid4_and_garbage_return_none() -> None:
    import uuid as uuid_mod
    from datetime import datetime

    from official_agent.state.pg import _uuid_timestamp_age_hours

    now = datetime.now(UTC).timestamp()
    assert _uuid_timestamp_age_hours("not-a-uuid", now) is None
    assert _uuid_timestamp_age_hours(str(uuid_mod.uuid4()), now) is None  # v4 → None


def test_purge_only_suspended_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挂起未恢复(interrupt 写入=最新事件)→ 三表清理;已恢复 → 不动(P1)。"""
    from official_agent.state import pg

    monkeypatch.setattr(pg, "_uuid_timestamp_age_hours", lambda cid, now: 30.0)

    class _Cursor:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            if sql.startswith("DELETE"):
                self.conn.deleted.append((sql.split("FROM")[1].strip().split(" ")[0], params[0]))
            elif "SELECT DISTINCT thread_id" in sql:
                self.conn.rows = [("suspended",), ("resumed",)]
            elif "FROM checkpoint_writes" in sql:
                tid = params[0]
                # suspended:interrupt 写入即最新事件;resumed:interrupt 是旧事件
                cp = "__int-old" if tid == "suspended" else "old-int-cp"
                self.conn.rows = [(cp,)]
            elif "max(checkpoint_id) FROM checkpoints" in sql:
                tid = params[0]
                cp = "__int-old" if tid == "suspended" else "newer-after-resume"
                self.conn.rows = [(cp,)]
            else:
                raise AssertionError(f"未预期查询:{sql[:60]}")

        def fetchall(self):
            return self.conn.rows

        def fetchone(self):
            return self.conn.rows[-1] if self.conn.rows else None

    class _Conn:
        def __init__(self):
            self.deleted = []
            self.rows = []

        def cursor(self):
            return _Cursor(self)

        def commit(self):
            pass

        def close(self):
            pass

    conn = _Conn()
    purged = pg.purge_expired_interrupts(max_age_hours=24, conn=conn)
    assert purged == 1  # 只有挂起未恢复的 suspended 被清
    assert any(t == "checkpoints" for t, _ in conn.deleted)
    assert any(tid == "resumed" for _, tid in conn.deleted) is False
