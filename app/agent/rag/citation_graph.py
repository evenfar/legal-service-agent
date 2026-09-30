"""法条引用图谱（mini GraphRAG）：案例 chunk ↔ 法条 chunk 的双向边。

法律文本天然是图：指导案例的 [相关法条] 字段显式引用《民法典》条号，
法条 chunk 的区间标签（第a-b条）可反向定位。向量检索只能算"语义相似"，
发现不了这种**结构化多跳关系**（案例→它引用的法条；法条→哪些案例适用）。

用法：get_citation_graph(kb_dir) 构建一次（解析零成本）；
neighbors(chunk_id) 双向查询；adaptive 侧做"邻居提名，rerank 终裁"的
保守扩展（图只给候选加机会分，不直接顶替结果——质量由精排兜底）。
"""

from __future__ import annotations

from pathlib import Path

from app.agent.rag.chunker import Chunk, chunk_markdown_dir
from app.agent.rag.lawref import article_range_of, parse_cited_articles

_graph_cache: dict[str, "CitationGraph"] = {}


class CitationGraph:
    def __init__(self):
        self.case_to_articles: dict[str, list[int]] = {}    # 案例chunk_id → [条号]
        self.article_to_chunks: dict[int, list[str]] = {}   # 条号 → 法条chunk_id
        self.article_to_cases: dict[int, list[str]] = {}    # 条号 → 引用它的案例chunk_id

    def build(self, chunks: list[Chunk]) -> None:
        pending_cases: list[tuple[str, list[int]]] = []
        for c in chunks:
            rng = article_range_of(c.section)
            if rng:
                lo, hi = rng
                for n in range(lo, hi + 1):
                    self.article_to_chunks.setdefault(n, []).append(c.chunk_id)
            elif "选编" in c.doc:                            # 案例文件
                arts = parse_cited_articles(c.text)
                if arts:
                    pending_cases.append((c.chunk_id, arts))
        for case_id, arts in pending_cases:
            self.case_to_articles[case_id] = arts
            for n in arts:
                self.article_to_cases.setdefault(n, []).append(case_id)

    def neighbors(self, chunk_id: str) -> set[str]:
        """案例→引用法条所在块；法条块→引用该条文的案例块。"""
        out: set[str] = set()
        arts = self.case_to_articles.get(chunk_id)
        if arts:                                             # 本块是案例
            for n in arts:
                out.update(self.article_to_chunks.get(n, []))
        for n, cases in self.article_to_cases.items():       # 本块含条文n?
            # 无法从id反查区间，用 article_to_chunks 判断归属
            if chunk_id in self.article_to_chunks.get(n, []):
                out.update(cases)
        out.discard(chunk_id)
        return out

    @property
    def edge_count(self) -> int:
        return sum(len(v) for v in self.case_to_articles.values())


def get_citation_graph(kb_dir: str) -> CitationGraph:
    g = _graph_cache.get(kb_dir)
    if g is None:
        g = CitationGraph()
        g.build(chunk_markdown_dir(Path(kb_dir)))
        _graph_cache[kb_dir] = g
    return g
