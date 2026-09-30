"""AdaptiveRetriever（Phase 2 核心实现）：策略路由 + 改写机制 + Rerank + Self-RAG。

架构（与 docs/04 设计稿一一对应）：

  query ──route()──▶ direct / hyde / fusion / stepback（启发式，0成本）
        ──任一策略──▶ 粗召回 top-20 ──rerank()──▶ top-3
        ──self_rag_gate()──▶ 相关性不足? 换fallback策略重试一次
        ──仍不足──▶ 显式返回 insufficient（检索层承认失败，不硬答）

预算硬约束：策略≤2、检索≤6、LLM调用≤4（防自适应变烧钱放大器）。

实现原则（面试点）：
- 组合不改写：包住 KnowledgeRetriever，不碰其契约与 search_knowledge 签名；
- 离线确定性：无 client 时四个改写全部走查表/模板，测试可精确断言；
- Rerank 规则版：条号精确命中 > 正文 bigram 重叠 > 向量分——纯代码可解释，
  真实模式可换 cross-encoder（接口不变）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from app.agent.rag.backends.base import RetrievedChunk
from app.agent.rag.chunker import chunk_markdown_dir  # noqa: F401 (文档引用)
from app.agent.rag.embedder import _tokens
from app.agent.rag.retriever import KnowledgeRetriever

MAX_STRATEGIES = 2
MAX_SEARCHES = 6
MAX_LLM_CALLS = 4
RECALL_K = 20
GATE_THRESHOLD = 0.12          # 相关性门槛：query bigram 在正文的重叠率
RRF_K = 60

# ---------------- 路由（启发式，零成本） ----------------

_LEGAL_TERMS = ("合同", "离婚", "抚养", "时效", "赔偿", "交通事故", "借款", "借贷",
                "侵权", "财产", "婚姻", "管辖", "上诉", "仲裁", "法条", "保证",
                "民法典", "抵押", "过错", "损害")
_ARTICLE_RE = re.compile(r"第\d+条|第[一二三四五六七八九十百千零]+条")
_STEPBACK_RE = re.compile(r"为什么|什么原理|什么意思|怎么理解|何为|是什么制度")
_MULTI_RE = re.compile(r"和|还有|以及|分别|都要|以及|又想")


def route(query: str) -> str:
    """启发式策略路由：返回 direct / hyde / fusion / stepback。"""
    if _ARTICLE_RE.search(query):
        return "direct"                      # 条号类：直查最准
    if _STEPBACK_RE.search(query):
        return "stepback"                    # 原理型：先退到一般规定
    if _MULTI_RE.search(query) and len(query) >= 10:
        return "fusion"                      # 多子问：拆多路召回融合
    legal_hits = sum(1 for t in _LEGAL_TERMS if t in query)
    if legal_hits == 0:
        return "hyde"                        # 口语零术语：假设文档对齐语体
    return "direct"


# ---------------- 离线确定性改写表（真实模式走 LLM purpose） ----------------

_HYDE_TEMPLATES = [
    (("借", "贷", "欠", "还钱"), "借款合同到期后借款人应当按照约定的期限返还借款；"
     "逾期不还的，出借人可以请求支付逾期利息；借款利率不得超过法律规定的上限。"),
    (("离婚", "抚养", "孩子"), "离婚时子女抚养按照最有利于未成年子女的原则判决，"
     "不满两周岁以由母亲直接抚养为原则；夫妻共同财产由双方协议处理。"),
    (("事故", "撞", "伤", "医疗费"), "侵害他人造成人身损害的，应当赔偿医疗费、护理费、"
     "交通费、营养费、住院伙食补助费等为治疗和康复支出的合理费用。"),
    (("时效", "过期", "多久"), "向人民法院请求保护民事权利的诉讼时效期间为三年，"
     "自权利人知道或者应当知道权利受到损害以及义务人之日起计算。"),
]

_FUSION_ASPECTS = {
    "借贷": ["借款返还期限", "逾期利息与利率上限", "借条与转账证据效力"],
    "离婚": ["夫妻共同财产分割", "子女抚养权归属", "离婚损害赔偿"],
    "事故": ["人身损害赔偿项目", "交通事故责任划分", "保险理赔顺序"],
    "时效": ["诉讼时效期间长度", "时效起算时间", "时效中断与中止"],
}

_STEPBACK_MAP = {
    "时效": "诉讼时效制度的一般规定",
    "离婚": "婚姻家庭编的一般规定",
    "赔偿": "侵权责任的一般规定与损害赔偿",
    "事故": "侵权责任的一般规定与损害赔偿",
    "合同": "合同编通则的一般规定",
    "借": "借款合同章的一般规定",
}


def offline_hyde(query: str) -> str:
    for keys, doc in _HYDE_TEMPLATES:
        if any(k in query for k in keys):
            return doc
    return query  # 无模板：退化为直查语义


def offline_fusion(query: str) -> list[str]:
    for topic, aspects in _FUSION_ASPECTS.items():
        if topic in query:
            return [query] + aspects[:2]
    return [query]


def offline_stepback(query: str) -> str:
    for key, abstract in _STEPBACK_MAP.items():
        if key in query:
            return abstract
    return query


# ---------------- RRF 融合与规则精排 ----------------

def rrf_merge(rankings: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """经典 Reciprocal Rank Fusion：score(d)=Σ 1/(k+rank)。纯代码、免调权。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def rerank_score(query: str, hit: RetrievedChunk) -> float:
    """规则精排（可解释、离线确定）：条号命中 > 正文bigram重叠 > 向量分。"""
    score = hit.score  # 向量基础分（hash下噪声大，仅作次级信号）
    m = _ARTICLE_RE.search(query)
    if m:
        from app.agent.tools.law import _normalize_article
        cn_no = _normalize_article(m.group(0))  # 千位条号转换逻辑复用法条工具
        if re.search(rf"^{re.escape(cn_no)}\s", hit.chunk.text, re.M):
            score += 100.0        # 条号(转中文)作为条文本体（行首），非案例引用
    sec_range = re.search(r"第(\d+)-(\d+)条", hit.chunk.section)
    if m and sec_range:
        num = re.search(r"\d+", m.group(0))
        if num and int(sec_range.group(1)) <= int(num.group()) <= int(sec_range.group(2)):
            score += 100.0                                # 条号落入块区间标签
    q_tokens = set(_tokens(query))
    overlap = len(q_tokens & set(_tokens(hit.chunk.text)))
    score += min(overlap, 12) * 2.0                       # 正文词重叠（封顶防长文偏置）
    return score


def relevance(query: str, chunk_text: str) -> float:
    """Self-RAG 相关性（离线规则版）：query bigram 在正文的覆盖率。"""
    q = set(_tokens(query))
    if not q:
        return 0.0
    return len(q & set(_tokens(chunk_text))) / len(q)


# ---------------- AdaptiveRetriever 本体 ----------------

@dataclass
class SearchStats:
    strategy: str = ""
    retried: bool = False
    relevant: bool = True
    llm_calls: int = 0
    searches: int = 0
    candidates: int = 0


class AdaptiveRetriever:
    """组合 KnowledgeRetriever；duck-type 兼容 .search/.size 原有调用。"""

    def __init__(self, base: KnowledgeRetriever, client=None):
        self.base = base
        self.client = client          # None=离线确定性改写；传入则真实模式走LLM
        self.last_stats = SearchStats()

    @property
    def embedder_model(self) -> str:
        return self.base.embedder_model

    @property
    def size(self) -> int:
        return self.base.size

    # ---- LLM/离线 改写分发 ----

    def _transform(self, strategy: str, query: str) -> tuple[str | list[str], int]:
        """返回 (改写结果, llm调用数)。"""
        if self.client is None:
            if strategy == "hyde":
                return offline_hyde(query), 0
            if strategy == "fusion":
                return offline_fusion(query), 0
            if strategy == "stepback":
                return offline_stepback(query), 0
            return query, 0
        purpose = {"hyde": "hyde", "fusion": "fusion_expand",
                   "stepback": "stepback"}.get(strategy)
        if purpose is None:
            return query, 0
        resp = self.client.chat([{"role": "user", "content": query}],
                                temperature=0.0, purpose=purpose)
        text = resp.content.strip()
        if strategy == "fusion":
            lines = [l.strip("- 、1.") for l in text.splitlines() if l.strip()]
            return (lines[:3] if len(lines) >= 2 else offline_fusion(query)), 1
        return (text if len(text) > 8 else offline_hyde(query)), 1

    # ---- 单策略执行 ----

    def _run_strategy(self, strategy: str, query: str, stats: SearchStats
                      ) -> list[RetrievedChunk]:
        transformed, llm = self._transform(strategy, query)
        stats.llm_calls += llm
        queries = transformed if isinstance(transformed, list) else [transformed]
        rankings: list[list[str]] = []
        pool: dict[str, RetrievedChunk] = {}
        for q in queries:
            if stats.searches >= MAX_SEARCHES:
                break
            hits = self.base.search(q, top_k=RECALL_K)
            stats.searches += 1
            stats.candidates = max(stats.candidates, len(hits))
            for h in hits:
                pool[h.chunk.chunk_id] = h
            rankings.append([h.chunk.chunk_id for h in hits])
        if not rankings:
            return []
        if len(rankings) == 1:
            ranked = sorted(pool.values(),
                            key=lambda h: -rerank_score(query, h))
        else:
            merged = rrf_merge(rankings)                 # 先RRF投票
            ranked = sorted(pool.values(),
                            key=lambda h: (-merged.get(h.chunk.chunk_id, 0.0),
                                           -rerank_score(query, h)))
        return ranked[:3]

    # ---- 对外入口（签名与 KnowledgeRetriever.search 兼容） ----

    def search(self, query: str, top_k: int = 3) -> list[RetrievedChunk]:
        stats = SearchStats()
        self.last_stats = stats
        strategy = route(query)
        stats.strategy = strategy

        def _gate_pass(chunks: list[RetrievedChunk]) -> bool:
            if not chunks:
                return False
            return relevance(query, chunks[0].chunk.text) >= GATE_THRESHOLD

        chunks = self._run_strategy(strategy, query, stats)
        if _gate_pass(chunks):
            return chunks[:top_k]

        # Self-RAG 不达标：换 fallback 策略重试一次（预算内）
        stats.retried = True
        fallback = {"direct": "hyde", "hyde": "direct",
                    "fusion": "direct", "stepback": "direct"}[strategy]
        if stats.llm_calls < MAX_LLM_CALLS and stats.searches < MAX_SEARCHES:
            retried = self._run_strategy(fallback, query, stats)
            if _gate_pass(retried):
                stats.strategy = f"{strategy}→{fallback}"
                return retried[:top_k]
            chunks = retried or chunks

        stats.relevant = False                            # 显式承认失败
        stats.strategy = f"{strategy}→{fallback}✗"
        return chunks[:top_k]
