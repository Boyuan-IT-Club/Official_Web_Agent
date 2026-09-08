"""客服知识库(RAG #134):面板录入 → 入库(分块+embedding)→ 检索 → 带引用问答。

- schema:KB 表自举(pgvector;source 层 + faq/doc 两内容表 + chunks + meta)
- chunking:结构分块(FAQ=1 块;正文按标题层级,上限截断)
- embedding:EMBED_* 独立配置组的 OpenAI-compatible 客户端
- store:条目 CRUD 与向量检索(kind=test 隔离;enabled/model_version 过滤)

R2 起:/admin/kb* 管理 API;R3 起:search_knowledge 工具进客服。
"""
