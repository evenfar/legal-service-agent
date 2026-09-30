"""BM25 词法检索器（混合检索的"关键词保精确"那一路）。

为什么法律场景必须要它：条号（"第680条"）、专名（"高空抛物"）、
术语（"口头订立"）在向量空间里可能漂移，在词法空间里是精确命中——
hash/弱语义 embedding 的 miss 大多能被 BM25 接住。
实现零依赖（Okapi BM25 完整公式：TF饱和k1/长度归一b/IDF平滑），
与 dense 路共享 chunk_id，产出排名列表直接进现有 rrf_merge。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from app.agent.rag.chunker import Chunk, chunk_markdown_dir


def lex_tokens(text: str) -> list[str]:
    """词法分词：ASCII词 + 中文bigram + 条号数字整体（第680条 的 '680'）。"""
    toks: list[str] = []
    for seg in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", text.lower()):
        if seg.isascii():
            toks.append(seg)
        elif len(seg) == 1:
            toks.append(seg)
        else:
            toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._chunks: list[Chunk] = []
        self._docs: dict[str, list[str]] = {}
        self._df: Counter = Counter()

    def build(self, chunks: list[Chunk]) -> None:
        self._chunks = list(chunks)
        self._docs = {}
        self._df = Counter()
        for c in chunks:
            toks = lex_tokens(c.text)
            self._docs[c.chunk_id] = toks
            self._df.update(set(toks))

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """返回 [(chunk_id, bm25分)] 降序。"""
        q = lex_tokens(query)
        n = len(self._docs)
        if n == 0 or not q:
            return []
        avgdl = sum(len(d) for d in self._docs.values()) / n
        scores: dict[str, float] = {}
        for doc_id, toks in self._docs.items():
            tf = Counter(toks)
            s = 0.0
            for qt in q:
                if qt not in tf:
                    continue
                idf = math.log((n - self._df[qt] + 0.5) / (self._df[qt] + 0.5) + 1.0)
                s += idf * (tf[qt] * (self.k1 + 1)) / (
                    tf[qt] + self.k1 * (1 - self.b + self.b * len(toks) / (avgdl or 1)))
            if s > 0:
                scores[doc_id] = s
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:top_k]

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        for c in self._chunks:
            if c.chunk_id == chunk_id:
                return c
        return None


_bm25_cache: dict[str, BM25Index] = {}


def get_bm25(kb_dir: str) -> BM25Index:
    """进程内缓存：同一知识库目录只建一次索引。"""
    idx = _bm25_cache.get(kb_dir)
    if idx is None:
        idx = BM25Index()
        idx.build(chunk_markdown_dir(Path(kb_dir)))
        _bm25_cache[kb_dir] = idx
    return idx
