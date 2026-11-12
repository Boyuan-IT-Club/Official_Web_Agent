"""草稿发布逻辑的单测:知识库写入打桩,验证三种草稿各自落到哪里、幂等与失败语义。"""

from typing import Any

import pytest

from official_agent.kb.store import KbValidationError, SourceInput
from official_agent.kb_ops import publish
from official_agent.kb_ops.publish import DraftNotPublishableError, check_publishable


def _draft(**over) -> dict[str, Any]:
    base = {
        "draft_id": 17,
        "kind": "new_faq",
        "topic": "社团会费",
        "question": "加入社团需要交费吗?",
        "answer": "加入社团不收取任何费用。",
        "target_source_id": None,
        "cycle_id": 12,
    }
    return {**base, **over}


def test_published_source_id_is_derived_from_the_draft() -> None:
    assert publish.published_source_id(17) == "kb_draft17"


def test_complete_drafts_are_publishable() -> None:
    check_publishable(_draft())
    check_publishable(_draft(kind="alt_question", answer="", target_source_id="kb_fee"))
    check_publishable(_draft(kind="missing", answer="管理员补写的答案。"))


@pytest.mark.parametrize(
    ("draft", "message"),
    [
        (_draft(kind="missing", answer=""), "还没有答案"),
        (_draft(answer="   "), "还没有答案"),
        (_draft(question=" "), "问题不能为空"),
        (_draft(kind="alt_question", answer="", target_source_id=None), "缺少目标条目"),
    ],
)
def test_incomplete_drafts_are_not_publishable(draft: dict, message: str) -> None:
    with pytest.raises(DraftNotPublishableError, match=message):
        check_publishable(draft)


async def test_new_faq_is_ingested_under_a_stable_source_id(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def ingest_source(source: SourceInput, *, embedder=None, source_id=None) -> str:
        seen.update(source=source, source_id=source_id, embedder=embedder)
        return source_id

    monkeypatch.setattr(publish.kb_store, "ingest_source", ingest_source)
    assert await publish.publish_draft(_draft(), actor="user:2") == "kb_draft17"
    assert seen["source_id"] == "kb_draft17"  # 重复发布覆盖同一条,不会多出一条
    assert seen["source"] == SourceInput(
        title="社团会费",
        type="faq",
        question="加入社团需要交费吗?",
        answer="加入社团不收取任何费用。",
        updated_by="user:2",
        cycle_id=12,
    )


async def test_missing_draft_with_a_human_answer_publishes_as_evergreen_faq(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def ingest_source(source: SourceInput, *, embedder=None, source_id=None) -> str:
        seen["source"] = source
        return source_id

    monkeypatch.setattr(publish.kb_store, "ingest_source", ingest_source)
    draft = _draft(kind="missing", answer="社团没有奖学金。", cycle_id=None)
    assert await publish.publish_draft(draft, actor="user:2") == "kb_draft17"
    assert seen["source"].answer == "社团没有奖学金。" and seen["source"].cycle_id is None


async def test_alt_question_is_added_to_its_target(monkeypatch) -> None:
    calls: list[tuple] = []

    async def add_alt_question(source_id: str, question: str, *, updated_by, embedder=None):
        calls.append((source_id, question, updated_by))
        return True

    async def never(*_a, **_k):
        raise AssertionError("补问法不该新建条目")

    monkeypatch.setattr(publish.kb_store, "add_alt_question", add_alt_question)
    monkeypatch.setattr(publish.kb_store, "ingest_source", never)
    draft = _draft(kind="alt_question", question="入社要不要交钱?", target_source_id="kb_fee")
    assert await publish.publish_draft(draft, actor="user:2") == "kb_fee"
    assert calls == [("kb_fee", "入社要不要交钱?", "user:2")]


async def test_publish_errors_propagate(monkeypatch) -> None:
    async def gone(*_a, **_k):
        raise KbValidationError("条目 kb_fee 不存在或不是 FAQ,无法添加问法")

    monkeypatch.setattr(publish.kb_store, "add_alt_question", gone)
    with pytest.raises(KbValidationError):
        await publish.publish_draft(
            _draft(kind="alt_question", target_source_id="kb_fee"), actor="user:2"
        )
