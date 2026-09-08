"""RAG #134 R1:结构分块纯逻辑单测(无 IO)。"""

from official_agent.kb.chunking import MAX_CHUNK_CHARS, embed_text, split_doc, split_faq


def test_faq_is_single_chunk_with_qa() -> None:
    chunks = split_faq("怎么加入社团?", "填写报名表即可。")
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert "怎么加入社团?" in chunks[0].text
    assert "填写报名表即可。" in chunks[0].text
    assert chunks[0].heading_path == ""


def test_doc_heading_builds_path_hierarchy() -> None:
    md = (
        "# 社团介绍\n我们是博远。\n## 部门\n技术部写代码。\n"
        "### 技术部职责\n开发官网。\n## 招新\n九月招新。"
    )
    chunks = split_doc(md)
    assert [c.heading_path for c in chunks] == [
        "社团介绍",
        "社团介绍 > 部门",
        "社团介绍 > 部门 > 技术部职责",
        "社团介绍 > 招新",
    ]
    assert chunks[2].text == "开发官网。"
    # 标题行本身不进正文
    assert all("##" not in c.text for c in chunks)


def test_doc_heading_exit_back_to_ancestor() -> None:
    md = "# A\n内容一\n## B\n内容二\n# C\n内容三"
    chunks = split_doc(md)
    assert chunks[2].heading_path == "C"  # 弹栈回一级
    assert chunks[1].heading_path == "A > B"


def test_doc_short_paragraphs_aggregate_into_one_chunk() -> None:
    md = "\n\n".join(f"这是第{i}段,内容很短。" for i in range(20))
    chunks = split_doc(md)
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0


def test_doc_oversized_paragraph_hard_split_respects_cap() -> None:
    long_text = "这是一个没有标点的超长段落" * 300  # ~3900 字符
    chunks = split_doc(long_text)
    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHUNK_CHARS for c in chunks)
    # ordinal 连续
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    # 内容不丢
    assert all(seg in "".join(c.text for c in chunks) for seg in ("这是一个没有标点的超长段落",))


def test_doc_sentence_aware_split_keeps_punctuation() -> None:
    sentence = "这是一句完整的话,带标点。"
    md = sentence * 80  # 960 字符,超 800
    chunks = split_doc(md)
    assert len(chunks) >= 2
    assert all(len(c.text) <= MAX_CHUNK_CHARS for c in chunks)
    assert chunks[0].text.endswith("。")  # 句界切分


def test_embed_text_prepends_heading_path() -> None:
    chunks = split_doc("# 指南\n正文内容")
    assert embed_text(chunks[0]) == "指南\n正文内容"
    plain = split_faq("问", "答")[0]
    assert embed_text(plain) == plain.text  # 无标题链不前缀
