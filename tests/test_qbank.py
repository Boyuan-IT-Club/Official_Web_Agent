"""qbank 存储 + 挑题路由测试(mock 连接/TestClient 权限闸)。"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from official_agent.state import qbank
from official_agent.web.app import create_app

# ── store(mock 连接) ────────────────────────────────────


def _mock_conn(fetchone=None, fetchall=None, rowcount=1):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    cur = conn.execute.return_value
    cur.fetchone.side_effect = (lambda: fetchone) if fetchone is not None else (lambda: None)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return conn


def test_save_qbank_version_increments(monkeypatch) -> None:
    conn = _mock_conn(fetchone={"v": 1})
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    version = qbank.save_qbank(
        resume_id=9,
        cycle_id=2026,
        source="repo_deep_dive",
        envelope={"schema_name": "evaluation_qbank/v2", "questions": []},
        prompt_version="evaluation_investigate/v1",
    )
    assert version == 2
    insert = next(
        c for c in conn.execute.call_args_list if "INSERT INTO interview_qbank" in c.args[0]
    )
    assert insert.args[1][2] == 2


def test_latest_qbank_deserializes_envelope(monkeypatch) -> None:
    row = {
        "resume_id": 9,
        "cycle_id": 2026,
        "qbank_version": 1,
        "source": "guided",
        "envelope": '{"questions": []}',
        "prompt_version": "v1",
        "created_at": None,
    }
    conn = _mock_conn(fetchone=row)
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    result = qbank.latest_qbank(9, 2026)
    assert result is not None
    assert result["envelope"] == {"questions": []}


def test_record_pick_and_list(monkeypatch) -> None:
    question_ref = '{"anchor": "architecture", "question": "架构?"}'
    conn = _mock_conn(
        fetchone={"id": 3},
        fetchall=[{"id": 3, "question_ref": question_ref}],
    )
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    pid = qbank.record_pick(
        resume_id=9,
        cycle_id=2026,
        interviewer_user_id=5,
        question_ref={"anchor": "architecture", "question": "架构?"},
        schedule_id=None,
    )
    assert pid == 3
    picks = qbank.list_picks(9, 2026)
    assert picks[0]["question_ref"]["anchor"] == "architecture"


# ── 路由权限 ────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, web_no_real_pg: None) -> TestClient:
    import contextlib
    from collections.abc import AsyncIterator

    @contextlib.asynccontextmanager
    async def _fake_checkpointer() -> AsyncIterator[None]:
        yield None

    # web_no_real_pg(tests/conftest.py):lifespan 自举/启动恢复不落真 PG。
    monkeypatch.setattr("official_agent.state.pg.get_checkpointer", _fake_checkpointer)
    with TestClient(create_app()) as c:
        yield c


def _install_resolve(monkeypatch: pytest.MonkeyPatch, codes: list[str]) -> None:

    async def _resolve(*_a: object, **_k: object) -> dict:
        return {
            "user_id": 5,
            "role": "member",
            "role_names": ["面试官"],
            "permission_codes": codes,
            "source": "web",
        }

    monkeypatch.setattr("official_agent.web.auth.resolve", _resolve)


_AUTH = {"Authorization": "Bearer tok"}


def test_qbank_rejects_plain_candidate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_resolve(monkeypatch, ["candidate:read:own"])
    resp = client.get("/api/agent/admin/evaluation/qbank?resume_id=1&cycle_id=2026", headers=_AUTH)
    assert resp.status_code == 403


def test_qbank_readable_by_interviewer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """面试官(interview:evaluate,无 resume:view)场景内可读题库。"""
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["interview:evaluate"])
    monkeypatch.setattr(
        ea.qbank_store,
        "latest_qbank",
        lambda r, c: {"resume_id": r, "cycle_id": c, "envelope": {"questions": []}},
    )
    resp = client.get("/api/agent/admin/evaluation/qbank?resume_id=1&cycle_id=2026", headers=_AUTH)
    assert resp.status_code == 200


def test_pick_records_with_interviewer_identity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["interview:evaluate"])
    captured: dict = {}

    def _fake_picks(**kw):
        captured.update(kw)
        return [1]

    # 路由必须走整批单事务的 record_picks,而不是逐条 record_pick
    monkeypatch.setattr(ea.qbank_store, "record_picks", _fake_picks)
    # 落库前必须经服务端权威解析;这里钉"解析结果才是落库内容"
    authoritative = {
        "group_index": 0,
        "group_kind": "repo",
        "role": "chain",
        "chain_index": 1,
        "layer_index": 0,
        "question": "架构?",
        "ref_id": "abc123",
    }
    monkeypatch.setattr(
        ea.qbank_store,
        "resolve_picks",
        lambda r, c, qs: [authoritative],
    )
    resp = client.post(
        "/api/agent/admin/evaluation/qbank/pick",
        headers=_AUTH,
        json={
            "resume_id": 9,
            "cycle_id": 2026,
            "schedule_id": 77,
            "questions": [{"anchor": "architecture", "question": "架构?"}],
        },
    )
    assert resp.status_code == 201
    assert resp.json() == {"picked": 1}
    assert captured["interviewer_user_id"] == 5  # 勾选人=当前面试官
    assert captured["schedule_id"] == 77  # 场次进 pick log
    assert captured["question_refs"] == [authoritative]  # 落库=权威引用,非客户端原文


def test_pick_writes_whole_batch_in_one_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多题勾选只调一次落库入口——逐条落库时部分失败会留下半截 pick log。"""
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["interview:evaluate"])
    calls: list = []
    resolved = [{"ref_id": "a"}, {"ref_id": "b"}, {"ref_id": "c"}]
    monkeypatch.setattr(ea.qbank_store, "resolve_picks", lambda r, c, qs: resolved)
    monkeypatch.setattr(
        ea.qbank_store,
        "record_picks",
        lambda **kw: (calls.append(kw), [1, 2, 3])[1],
    )
    resp = client.post(
        "/api/agent/admin/evaluation/qbank/pick",
        headers=_AUTH,
        json={
            "resume_id": 9,
            "cycle_id": 2026,
            "questions": [{"question": q["ref_id"]} for q in resolved],
        },
    )
    assert resp.status_code == 201
    assert resp.json() == {"picked": 3}
    assert len(calls) == 1, "整批一次落库,不逐条开事务"
    assert calls[0]["question_refs"] == resolved


# ── v2 信封门禁 + pickable 扁平视图 ──


def test_save_qbank_rejects_non_v2_envelope(monkeypatch) -> None:
    """非 v2 信封拒绝入库,并提示清理旧数据。"""
    conn = _mock_conn(fetchone={"v": 1})
    monkeypatch.setattr(qbank, "_conn", lambda: conn)
    with pytest.raises(ValueError, match="evaluation_qbank/v2"):
        qbank.save_qbank(
            resume_id=9,
            cycle_id=2026,
            source="repo_deep_dive",
            envelope={"repo_summary": "旧 v1 形状", "questions": []},
            prompt_version="evaluation_investigate/v1",
        )


def test_flatten_v2_pickable_covers_all_roles() -> None:
    envelope = {
        "schema_name": "evaluation_qbank/v2",
        "groups": [
            {
                "group": "repo",
                "owner": "me",
                "mode": "repo_deep_dive",
                "group_inner_note": "不该出现在扁平视图",
                "entry": {
                    "category": "C1_背景与动机",
                    "question": "入口题?",
                    "evidence": {"path": "README.md", "note": ""},
                    "time_minutes": 3,
                },
                "chains": [
                    {
                        "category": "C4_实现细节拷打",
                        "theme": "核心模块",
                        "layers": [
                            {"question": "L1", "expected_signal": "s1"},
                            {"question": "L2", "expected_signal": "s2"},
                        ],
                    }
                ],
                "reserves": [
                    {
                        "category": "C6_难点与调试",
                        "question": "备选?",
                        "evidence": {"path": "", "note": ""},
                        "time_minutes": 3,
                    }
                ],
            },
            {
                "group": "awards",
                "mode": "award_brief",
                "questions": [{"question": "奖项过程题", "anchor": "guided"}],
            },
        ],
    }
    from official_agent.state.qbank import flatten_v2_pickable

    flat = flatten_v2_pickable(envelope, resume_id=9, cycle_id=2026, qbank_version=1)
    roles = [(f["group_kind"], f["role"], f["question"]) for f in flat]
    assert ("repo", "entry", "入口题?") in roles
    assert ("repo", "chain", "L1") in roles and ("repo", "chain", "L2") in roles
    assert ("repo", "reserve", "备选?") in roles
    assert ("awards", "question", "奖项过程题") in roles
    chain_ref = next(f for f in flat if f["role"] == "chain")
    assert chain_ref["chain_index"] == 0 and chain_ref["layer_index"] == 0
    assert chain_ref["expected_signal"] == "s1"
    entry_ref = next(f for f in flat if f["role"] == "entry")
    assert entry_ref["evidence_path"] == "README.md"


# ── 稳定 ref_id + 服务端权威解析 ──


def _dup_envelope() -> dict:
    """两条 chain 同文题 + 降级组 + 多项目组,覆盖三个场景。"""
    return {
        "schema_name": "evaluation_qbank/v2",
        "groups": [
            {
                "group": "repo",
                "qbank_v2": {
                    "group": {
                        "entry": {
                            "category": "C1",
                            "question": "入口题?",
                            "evidence": {"path": "README.md"},
                        },
                        "chains": [
                            {
                                "category": "C4",
                                "theme": "模块A",
                                "layers": [{"question": "同文题?", "expected_signal": "信号A"}],
                            },
                            {
                                "category": "C4",
                                "theme": "模块B",
                                "layers": [{"question": "同文题?", "expected_signal": "信号B"}],
                            },
                        ],
                        "reserves": [],
                    }
                },
            },
            {
                "group": "awards",
                "questions": [{"question": "奖项过程题?", "anchor": "guided"}],
            },
        ],
    }


def test_flatten_ref_id_stable_and_disambiguates_duplicates() -> None:
    """ref_id 确定性;同文题靠索引得不同 id;版本变了 id 跟着变。"""
    env = _dup_envelope()
    flat = qbank.flatten_v2_pickable(env, resume_id=9, cycle_id=2026, qbank_version=3)
    dups = [e for e in flat if e["question"] == "同文题?"]
    assert len(dups) == 2
    assert dups[0]["ref_id"] != dups[1]["ref_id"], "同文题必须不同 ref_id"
    assert dups[0]["chain_index"] == 0 and dups[1]["chain_index"] == 1
    again = qbank.flatten_v2_pickable(env, resume_id=9, cycle_id=2026, qbank_version=3)
    assert [e["ref_id"] for e in again] == [e["ref_id"] for e in flat], "ref_id 确定性"
    newer = qbank.flatten_v2_pickable(env, resume_id=9, cycle_id=2026, qbank_version=4)
    assert newer[0]["ref_id"] != flat[0]["ref_id"], "版本戳参与 ref_id"
    # 降级组(questions 形状)也有 ref_id
    assert any(e["role"] == "question" and e.get("ref_id") for e in flat)


def test_resolve_picks_binds_authoritative_entry(monkeypatch) -> None:
    """ref_id 命中 → 返回服务端权威条目;客户端伪造字段被替换。"""
    env = _dup_envelope()
    flat = qbank.flatten_v2_pickable(env, resume_id=9, cycle_id=2026, qbank_version=3)
    target = [e for e in flat if e["question"] == "同文题?"][1]  # 模块B 的那条
    monkeypatch.setattr(qbank, "latest_qbank", lambda r, c: {"envelope": env, "qbank_version": 3})
    resolved = qbank.resolve_picks(9, 2026, [{"question": "同文题?", "ref_id": target["ref_id"]}])
    assert len(resolved) == 1
    assert resolved[0]["ref_id"] == target["ref_id"]
    assert resolved[0]["expected_signal"] == "信号B"  # 绑定到正确来源


def test_resolve_picks_rejects_duplicate_text_without_ref(monkeypatch) -> None:
    """同文多题的旧形状引用必拒——这正是按题文反查串源的根源。"""
    env = _dup_envelope()
    monkeypatch.setattr(qbank, "latest_qbank", lambda r, c: {"envelope": env, "qbank_version": 3})
    with pytest.raises(LookupError, match="不唯一"):
        qbank.resolve_picks(9, 2026, [{"question": "同文题?"}])


def test_resolve_picks_rejects_unknown_question(monkeypatch) -> None:
    env = _dup_envelope()
    monkeypatch.setattr(qbank, "latest_qbank", lambda r, c: {"envelope": env, "qbank_version": 3})
    with pytest.raises(LookupError):
        qbank.resolve_picks(9, 2026, [{"question": "题库里没有的题"}])


def test_resolve_picks_legacy_shape_unique_text_binds(monkeypatch) -> None:
    """旧形状 {anchor, question} 在唯一命中时仍可用(兼容在役前端)。"""
    env = _dup_envelope()
    monkeypatch.setattr(qbank, "latest_qbank", lambda r, c: {"envelope": env, "qbank_version": 3})
    resolved = qbank.resolve_picks(9, 2026, [{"anchor": "guided", "question": "奖项过程题?"}])
    assert len(resolved) == 1
    assert resolved[0]["group_kind"] == "awards"
    assert resolved[0]["role"] == "question"
    assert resolved[0].get("ref_id")


def test_resolve_picks_no_qbank(monkeypatch) -> None:
    monkeypatch.setattr(qbank, "latest_qbank", lambda r, c: None)
    with pytest.raises(LookupError, match="暂无预置题库"):
        qbank.resolve_picks(9, 2026, [{"question": "任意"}])


# ── 题库 404 文案:区分"从未跑过 / 进行中 / 生成失败" ──────
# 生产事故回归:权限 403 炸掉的题库线被展示成"暂无题库",看似没数据,
# 实则生成失败且原因不可见。404 必须联查 job 给出真实原因与出路。


def test_qbank_404_without_any_job_says_never_ran(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import evaluation
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["resume:audit"])
    monkeypatch.setattr(ea.qbank_store, "latest_qbank", lambda r, c: None)
    monkeypatch.setattr(evaluation, "latest_job", lambda r, c: None)
    resp = client.get("/api/agent/admin/evaluation/qbank?resume_id=1&cycle_id=2026", headers=_AUTH)
    assert resp.status_code == 404
    assert "尚未跑过 AI 初筛" in resp.json()["detail"]


def test_qbank_404_failed_job_carries_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from official_agent.state import evaluation
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["resume:audit"])
    monkeypatch.setattr(ea.qbank_store, "latest_qbank", lambda r, c: None)
    monkeypatch.setattr(
        evaluation,
        "latest_job",
        lambda r, c: {
            "status": "succeeded",
            "qbank_status": "failed",
            "qbank_error": "BackendError: 权限不足(code 2101)",
        },
    )
    resp = client.get("/api/agent/admin/evaluation/qbank?resume_id=1&cycle_id=2026", headers=_AUTH)
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "生成失败" in detail
    assert "权限不足(code 2101)" in detail  # 真实原因直达评审,不再"暂无题库"
    assert "重新触发" in detail


def test_qbank_generating_job_returns_flag_not_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """job 进行中 → 200 + generating 标记,而非 404 报错。

    用户在出题窗口内打开抽屉是常态:404 会走前端报错路径,把
    "正在出题"伪装成故障。复跑中的 job 残留上一轮 qbank_status=failed
    也不影响——生成中就是生成中。"""
    from official_agent.state import evaluation
    from official_agent.web import evaluation_admin as ea

    _install_resolve(monkeypatch, ["resume:audit"])
    monkeypatch.setattr(ea.qbank_store, "latest_qbank", lambda r, c: None)
    monkeypatch.setattr(
        evaluation,
        "latest_job",
        lambda r, c: {"status": "running", "qbank_status": "failed", "qbank_error": "旧"},
    )
    resp = client.get("/api/agent/admin/evaluation/qbank?resume_id=1&cycle_id=2026", headers=_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["generating"] is True
    assert "envelope" not in body


# ── 可挑题视图:并入审题意见与相似度标注 ───────────────────


def _annotated_envelope() -> dict:
    """两条线的题库:仓线(入口 + 一条两层的链 + 一道备选)与评测线(两道题)。

    标注都挂在题组旁边或信封顶层,题目对象里没有。
    """
    return {
        "schema_name": "evaluation_qbank/v2",
        "groups": [
            {
                "group": "repo",
                "repo": "me/a",
                "qbank_v2": {
                    "group": {
                        "entry": {"category": "C1_背景与动机", "question": "为什么做?"},
                        "chains": [
                            {
                                "category": "C4_实现细节拷打",
                                "theme": "限流",
                                "layers": [{"question": "按什么限?"}, {"question": "并发时呢?"}],
                            }
                        ],
                        "reserves": [{"category": "C9_变更条件", "question": "流量 ×10 呢?"}],
                    },
                    "review": {
                        "status": "reviewed",
                        "items": [
                            {"key": "entry", "verdict": "keep", "reasons": []},
                            {"key": "chain:0:1", "verdict": "revise", "reasons": ["recitable"]},
                        ],
                    },
                },
            },
            {
                "group": "autograding",
                "questions": [
                    {"anchor": "guided", "question": "超时那条怎么查的?"},
                    {"anchor": "guided", "question": "边界用例为什么挂?"},
                ],
                "review": {
                    "status": "reviewed",
                    "items": [{"key": "question:1", "verdict": "drop", "reasons": ["generic"]}],
                },
            },
        ],
        "similarity": {
            "embed_model": "test-embed",
            "items": [
                {
                    "group_index": 1,
                    "key": "question:0",
                    "duplicate_of": {"group_index": 0, "key": "chain:0:0"},
                    "generic_resumes": 0,
                },
                {"group_index": 0, "key": "reserve:0", "duplicate_of": None, "generic_resumes": 4},
            ],
        },
    }


def _flat(envelope: dict) -> dict[tuple[int, str], dict]:
    entries = qbank.flatten_v2_pickable(envelope, resume_id=9, cycle_id=2026, qbank_version=2)
    return {(e["group_index"], qbank.entry_key(e)): e for e in entries}


def test_pickable_entries_carry_review_and_similarity() -> None:
    """标注按「组下标 + 组内编号」对回题目;没被标注的题不带这两个键。"""
    flat = _flat(_annotated_envelope())

    assert flat[(0, "entry")]["review"] == {"verdict": "keep", "reasons": []}
    assert flat[(0, "chain:0:1")]["review"] == {"verdict": "revise", "reasons": ["recitable"]}
    assert flat[(1, "question:1")]["review"] == {"verdict": "drop", "reasons": ["generic"]}
    assert "review" not in flat[(0, "chain:0:0")]  # 审题者没给这道题意见
    assert "review" not in flat[(1, "question:0")]

    # 重复指向本包里问同一件事、证据更强的那道题,用的是稳定题引用 id
    assert flat[(1, "question:0")]["similarity"] == {
        "duplicate_of_ref_id": flat[(0, "chain:0:0")]["ref_id"],
        "generic_resumes": 0,
    }
    assert flat[(0, "reserve:0")]["similarity"] == {
        "duplicate_of_ref_id": None,
        "generic_resumes": 4,
    }
    assert "similarity" not in flat[(0, "entry")]


def test_annotations_do_not_change_entries_or_ref_ids() -> None:
    """去掉标注后,可挑题条目与题引用 id 和带标注时完全一致(除了那两个键)。"""
    annotated = _annotated_envelope()
    plain = _annotated_envelope()
    plain.pop("similarity")
    plain["groups"][0]["qbank_v2"].pop("review")
    plain["groups"][1].pop("review")

    with_notes, without_notes = _flat(annotated), _flat(plain)

    assert list(with_notes) == list(without_notes)
    for locator, entry in without_notes.items():
        assert "review" not in entry and "similarity" not in entry
        stripped = {
            k: v for k, v in with_notes[locator].items() if k not in ("review", "similarity")
        }
        assert stripped == entry


def test_pick_by_ref_id_returns_entry_with_its_review(monkeypatch) -> None:
    """按 ref_id 勾选仍然精确命中;落进 pick log 的权威条目带着当时的审题意见。"""
    envelope = _annotated_envelope()
    monkeypatch.setattr(
        qbank,
        "latest_qbank",
        lambda r, c: {"envelope": envelope, "qbank_version": 2},
    )
    target = _flat(envelope)[(0, "chain:0:1")]

    (resolved,) = qbank.resolve_picks(
        9, 2026, [{"ref_id": target["ref_id"], "question": target["question"]}]
    )

    assert resolved["ref_id"] == target["ref_id"]
    assert resolved["review"] == {"verdict": "revise", "reasons": ["recitable"]}
