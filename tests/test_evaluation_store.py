"""B1 evaluation_scorecard 数据面单测:mock 连接验证 SQL/版本递增语义。"""

from unittest.mock import MagicMock

import pytest

from official_agent.state import evaluation


def _mock_conn(fetchone=None, fetchall=None, rowcount=1):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (lambda: fetchone) if fetchone is not None else (lambda: None)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return conn


def test_save_scorecard_version_increments(monkeypatch) -> None:
    """重跑版本递增:版本 = 现存最大+1,旧版保留(#124)。"""
    conn = _mock_conn(fetchone={"v": 2})
    monkeypatch.setattr(evaluation, "_conn", lambda: conn)
    version = evaluation.save_scorecard(
        {"total": 71.5, "hard_zero": False},
        resume_id=9,
        cycle_id=2026,
        prompt_version="evaluation_scoring/v1",
    )
    assert version == 3
    sqls = [c.args[0] for c in conn.execute.call_args_list]
    assert any("COALESCE(MAX(card_version), 0)" in s for s in sqls)
    insert = next(
        c for c in conn.execute.call_args_list if "INSERT INTO evaluation_scorecard" in c.args[0]
    )
    params = insert.args[1]
    assert params[0] == 9 and params[1] == 2026 and params[2] == 3
    assert params[4] is False and params[5] == 71.5  # hard_zero / total 冗余提列
    assert '"total": 71.5' in params[6]  # 卡 JSONB


def test_set_status_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="非法卡态"):
        evaluation.set_scorecard_status(1, 2026, 1, "pending")


def test_set_status_scoped_by_version(monkeypatch) -> None:
    conn = _mock_conn(rowcount=1)
    monkeypatch.setattr(evaluation, "_conn", lambda: conn)
    assert evaluation.set_scorecard_status(9, 2026, 2, "adopted") is True
    sql, params = conn.execute.call_args.args
    assert "status = %s" in sql
    assert params == ("adopted", 9, 2026, 2)
