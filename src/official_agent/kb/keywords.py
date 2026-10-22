"""检索关键词:从用户问题里抽出「值得字面匹配」的词,给混合检索的关键词一路用。

向量检索擅长「意思相近」,但对专有名词不敏感:问「计拔的同学能报项目部吗」,
「计拔」「项目部」这种字面词在向量里只是一点点信号。关键词一路补的就是这块——
条目里字面出现了这些词,就算向量分数不高也值得召回。

中文没有空格,用 jieba 精确模式分词,再去掉疑问词、虚词和单字;社团专有名词
预先加进词典,保证不被切碎。纯函数(除首次加载词典),检索与测试共用。
"""

from __future__ import annotations

import logging
import threading
import warnings

#: 社团专有名词:加进分词词典,避免「项目部」被切成「项目/部」。
_GLOSSARY = (
    "博远",
    "博远信息技术社",
    "计拔",
    "技术部",
    "项目部",
    "媒体部",
    "综合部",
    "招新",
    "保研",
    "面试官",
)

#: 不参与字面匹配的词:疑问词、虚词、口语填充。它们在任何条目里都可能出现,
#: 留着只会让「字面命中」失去区分度。
_STOPWORDS = frozenset(
    {
        "什么", "什么样", "怎么", "怎么样", "怎样", "如何", "哪些", "哪个", "哪里",
        "多少", "多久", "为什么", "是否", "可以", "能否", "能不能", "可不可以",
        "不是", "没有", "有没有", "一下", "一些", "这个", "那个", "我们", "你们",
        "他们", "自己", "需要", "要求", "请问", "知道", "想要", "的话", "就是",
        "还是", "或者", "以及", "平时", "一般", "现在", "时候",
    }
)

_lock = threading.Lock()
_jieba = None


def _tokenizer():
    """懒加载 jieba(首次约 1 秒建前缀词典),加载时补社团词表。"""
    global _jieba
    if _jieba is None:
        with _lock:
            if _jieba is None:
                # jieba 源码里有旧式转义,新版 Python 编译时报 SyntaxWarning;与本仓无关
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    import jieba
                jieba.setLogLevel(logging.WARNING)
                for word in _GLOSSARY:
                    jieba.add_word(word)
                _jieba = jieba
    return _jieba


def query_terms(text: str, *, max_terms: int = 8) -> list[str]:
    """问题 → 去重后的关键词(保序)。只留两字及以上、不在停用表里的词。"""
    seen: set[str] = set()
    terms: list[str] = []
    for raw in _tokenizer().lcut(text or ""):
        word = raw.strip()
        if len(word) < 2 or word in _STOPWORDS or word in seen:
            continue
        if not any(ch.isalnum() for ch in word):
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) >= max_terms:
            break
    return terms
