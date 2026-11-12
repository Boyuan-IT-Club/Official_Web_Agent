"""校准案例数据面的真库集成测试:建表幂等、向量往返、停用、去重、按周期冻结的水位、收割的取数。

运行门槛同其他数据层集成档:POSTGRES_URL 可连才跑。每个用例用独立的嵌入模型名与周期号
隔离数据,结束时删掉自己写的行。
"""

import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.pg_helpers import PG_URL, pg_available, point_settings_at_pg  # noqa: E402

from official_agent.state import audit  # noqa: E402
from official_agent.state import evaluation as ev_store  # noqa: E402
from official_agent.state.evaluation import bootstrap, calibration_store  # noqa: E402

pytestmark = pytest.mark.skipif(not pg_available(), reason="需要真 PostgreSQL(POSTGRES_URL)")


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_tables():
    previous = point_settings_at_pg()
    ev_store.reset_pool()
    try:
        ev_store.ensure_evaluation_scorecard_ready()
        audit.ensure_audit_table()
        yield
    finally:
        point_settings_at_pg(previous)


@pytest.fixture(autouse=True)
def _point_settings_at_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    from official_agent.config import get_settings

    monkeypatch.setattr(get_settings(), "postgres_url", PG_URL)
    ev_store.reset_pool()


@pytest.fixture
def model() -> str:
    """每个用例一个嵌入模型名:检索只取同一个模型的行,天然隔离;结束时按它清场。"""
    name = f"test-embed-{uuid.uuid4().hex[:12]}"
    yield name
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM evaluation_calibration_case WHERE embed_model = %s", (name,))


@pytest.fixture
def cycle_id() -> int:
    cid = uuid.uuid4().int % 2_000_000_000 + 1
    yield cid
    with psycopg.connect(PG_URL) as conn:
        conn.execute("DELETE FROM evaluation_scorecard WHERE cycle_id = %s", (cid,))
        conn.execute(
            "DELETE FROM agent_audit_log WHERE (action->>'cycle_id')::text = %s", (str(cid),)
        )


def _case(item: str = "技术基础", *, met: bool = True) -> dict:
    return {
        "checklist": "技术部",
        "item": item,
        "met": met,
        "quote": "用 Flask 写了课程表小程序" if met else "",
        "reason": "有做出来的东西" if met else "只列了技术名词",
    }


def _add(model: str, case: dict | None = None, **meta) -> int | None:
    meta.setdefault("origin", "seed")
    return calibration_store.add_case(
        case or _case(), embedding=[0.25, -0.5, 1.0], embed_model=model, **meta
    )


def test_bootstrap_is_idempotent_and_needs_no_vector_extension() -> None:
    """建表不依赖 pgvector 扩展(向量存成 real[]),重复执行无害。"""
    with psycopg.connect(PG_URL, row_factory=psycopg.rows.dict_row) as conn:
        calibration_store.ensure_calibration_table(conn)
        calibration_store.ensure_calibration_table(conn)
        row = conn.execute(
            "SELECT data_type, udt_name FROM information_schema.columns "
            "WHERE table_name = 'evaluation_calibration_case' AND column_name = 'embedding'"
        ).fetchone()
    assert row == {"data_type": "ARRAY", "udt_name": "_float4"}


def test_first_use_creates_the_table_without_a_startup_hook(model: str) -> None:
    """这张表没有挂在服务启动的自举里:每个进程第一次用到时自己建。"""
    bootstrap._done.discard("calibration")

    assert calibration_store.list_cases(checklist="不存在的清单") == []
    assert "calibration" in bootstrap._done


def test_case_round_trips_with_its_vector_and_listing_hides_the_vector(model: str) -> None:
    case_id = _add(model, created_by=7)

    (listed,) = [
        c for c in calibration_store.list_cases(checklist="技术部") if c["case_id"] == case_id
    ]
    assert listed["origin"] == "seed" and listed["created_by"] == 7
    assert listed["quote"] == "用 Flask 写了课程表小程序"
    assert listed["retired_at"] is None
    assert "embedding" not in listed

    (loaded,) = calibration_store.load_cases_for_retrieval(model)
    assert loaded["case_id"] == case_id
    assert loaded["embedding"] == [0.25, -0.5, 1.0]
    assert loaded["source_cycle_id"] is None and loaded["source_resume_id"] is None


def test_retrieval_only_sees_cases_from_the_same_embedding_model(model: str) -> None:
    """不同模型算出的向量不可比:换了嵌入模型,旧向量的案例就不再参与检索。"""
    _add(model)

    assert len(calibration_store.load_cases_for_retrieval(model)) == 1
    assert calibration_store.load_cases_for_retrieval(f"{model}-v2") == []


def test_retired_case_leaves_retrieval_but_stays_on_record(model: str) -> None:
    kept, gone = _add(model), _add(model, _case("分享意愿"))

    assert calibration_store.retire_case(gone) is True
    assert calibration_store.retire_case(gone) is False  # 已经停用
    assert calibration_store.retire_case(-1) is False

    assert [c["case_id"] for c in calibration_store.load_cases_for_retrieval(model)] == [kept]
    visible = {c["case_id"] for c in calibration_store.list_cases(checklist="技术部")}
    everything = {
        c["case_id"] for c in calibration_store.list_cases(checklist="技术部", include_retired=True)
    }
    assert gone not in visible and gone in everything


def test_listing_filters_by_checklist_and_item(model: str) -> None:
    base, share = _add(model), _add(model, _case("分享意愿", met=False))

    by_item = calibration_store.list_cases(checklist="技术部", item="分享意愿")
    assert share in {c["case_id"] for c in by_item}
    assert base not in {c["case_id"] for c in by_item}
    assert all(c["item"] == "分享意愿" for c in by_item)


def test_watermark_is_the_newest_case_number(model: str) -> None:
    first = _add(model)
    assert calibration_store.current_watermark() == first
    second = _add(model)
    assert calibration_store.current_watermark() == second > first


def test_same_verdict_of_the_same_card_is_stored_once(model: str, cycle_id: int) -> None:
    """重复收割同一个周期不产生重复案例;种子判例不受这条约束。"""
    source = {"origin": "adopted_card", "source_cycle_id": cycle_id, "source_resume_id": 9001}

    assert _add(model, **source) is not None
    assert _add(model, **source) is None
    assert _add(model, _case("分享意愿"), **source) is not None  # 同一张卡的另一项
    assert _add(model, **{**source, "source_resume_id": 9002}) is not None
    assert _add(model) is not None and _add(model) is not None  # 两条内容相同的种子判例
    assert len(calibration_store.load_cases_for_retrieval(model)) == 5


def _save(cycle_id: int, resume_id: int, card: dict) -> int:
    return ev_store.save_scorecard(
        card, resume_id=resume_id, cycle_id=cycle_id, prompt_version="evaluation_scoring/v7"
    )


def test_cycle_calibration_set_comes_from_the_first_card(cycle_id: int) -> None:
    """周期开跑时冻结的水位以最早落库的那张卡为准;之后的卡记了别的也不改变它。"""
    assert ev_store.cycle_calibration_set(cycle_id) is None  # 周期里还没有卡

    _save(cycle_id, 9101, {"panel": {"mode": "critic", "calibration": {"set": 12}}})
    assert ev_store.cycle_calibration_set(cycle_id) == 12
    _save(cycle_id, 9100, {"panel": {"mode": "critic", "calibration": {"set": 40}}})
    assert ev_store.cycle_calibration_set(cycle_id) == 12


def test_cycle_started_without_calibration_stays_without_it(cycle_id: int) -> None:
    """第一张卡没记水位(单评委模式,或启用校准案例之前出的卡)→ 本周期不用校准案例。"""
    _save(cycle_id, 9110, {"panel": {"mode": "critic"}})
    _save(cycle_id, 9111, {"panel": {"mode": "critic", "calibration": {"set": 7}}})

    assert ev_store.cycle_calibration_set(cycle_id) == 0


def test_harvest_inputs_are_adopted_latest_cards_and_the_reviewers_scores(cycle_id: int) -> None:
    v1 = _save(cycle_id, 9201, {"match_score": 3.0})
    ev_store.set_scorecard_status(9201, cycle_id, v1, "adopted")
    _save(cycle_id, 9201, {"match_score": 8.0})  # 采纳之后又重评:最新版是 draft,不算
    v = _save(cycle_id, 9202, {"match_score": 6.0})
    ev_store.set_scorecard_status(9202, cycle_id, v, "adopted")
    _save(cycle_id, 9203, {"match_score": 5.0})  # 没人处理过

    cards = calibration_store.list_adopted_cards(cycle_id)
    assert [(c["resume_id"], c["card"]["match_score"]) for c in cards] == [(9202, 6.0)]

    def adopt(resume_id: int, score: int, op: str = "adopt_scorecard") -> None:
        audit.write_audit(
            thread_id=f"eval:{cycle_id}:t",
            acting_user_id=7,
            channel="evaluation",
            agent="evaluation-reviewer",
            action={"op": op, "resume_id": resume_id, "cycle_id": cycle_id, "score": score},
            decision="u7:adopt",
            result="采纳",
        )

    adopt(9202, 60)
    adopt(9202, 75)  # 同一份简历再次采纳:取最后一次
    adopt(9203, 88, op="adopt_vote_cast")  # 采纳受理的意图记录不算结果

    assert calibration_store.adopted_human_scores(cycle_id) == {9202: 75}
