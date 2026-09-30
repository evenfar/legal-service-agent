"""Phase 2 自适应检索栈测试：路由 / 改写 / RRF / 精排 / Self-RAG门 / 预算 / 对比护栏。"""

from __future__ import annotations

import json

import pytest

from app.agent.rag.adaptive import (AdaptiveRetriever, GATE_THRESHOLD,
                                    MAX_LLM_CALLS, MAX_SEARCHES, offline_fusion,
                                    offline_hyde, offline_stepback, rrf_merge,
                                    rerank_score, relevance, route)
from app.config.settings import Settings


# ================= 路由 =================

class TestRoute:
    def test_article_number_direct(self):
        assert route("民法典第680条怎么规定") == "direct"

    def test_why_stepback(self):
        assert route("为什么诉讼时效是三年") == "stepback"

    def test_multi_aspect_fusion(self):
        assert route("离婚时房子和孩子怎么分") == "fusion"

    def test_colloquial_hyde(self):
        assert route("朋友借钱拖着不还电话也不接") == "hyde"

    def test_legal_term_direct(self):
        assert route("交通事故责任如何划分") == "direct"


# ================= 离线改写 =================

class TestOfflineTransforms:
    def test_hyde_template_hit(self):
        doc = offline_hyde("朋友借钱拖着不还")
        assert "返还借款" in doc  # 语体已对齐条文

    def test_hyde_fallback_returns_query(self):
        assert offline_hyde("量子纠缠") == "量子纠缠"

    def test_fusion_aspects(self):
        qs = offline_fusion("离婚时房子和孩子怎么分")
        assert len(qs) == 3 and "离婚" in qs[0]

    def test_stepback_mapping(self):
        assert "一般规定" in offline_stepback("为什么诉讼时效是三年")


# ================= RRF 与精排 =================

class TestRRF:
    def test_math(self):
        scores = rrf_merge([["a", "b"], ["b", "c"]], k=60)
        # b: 第1路第2(1/62)+第2路第1(1/61)；a: 第1路第1(1/61)；c: 第2路第2(1/62)
        assert scores["b"] == pytest.approx(1 / 61 + 1 / 62)
        assert scores["a"] == pytest.approx(1 / 61)
        assert scores["c"] == pytest.approx(1 / 62)

    def test_cross_query_voting_wins(self):
        scores = rrf_merge([["a", "b", "x"], ["b", "c", "x"], ["d", "b", "x"]])
        assert scores["b"] == max(scores.values())  # 三路都见者登顶


class TestRerank:
    def test_article_number_beats_citation_and_noise(self):
        """纯函数测试：条文本体(行首条号) > 案例引用 > 向量高分噪声块。"""
        from app.agent.rag.backends.base import RetrievedChunk
        from app.agent.rag.chunker import Chunk
        body = RetrievedChunk(score=0.10, chunk=Chunk(
            "a#00-00", "民法典-婚姻家庭", "家庭关系（第1055-1064条）",
            "【民法典-婚姻家庭#家庭关系（第1055-1064条）】" + chr(10)
            + "第一千零六十二条 夫妻在婚姻关系存续期间所得的工资为共同财产。"))
        citation = RetrievedChunk(score=0.90, chunk=Chunk(
            "b#00-00", "指导案例选编", "指导案例50号",
            "【指导案例选编#指导案例50号】……[相关法条] 《民法典》第1062条……"))
        noise = RetrievedChunk(score=0.50, chunk=Chunk(
            "c#00-00", "民法典-合同", "运输合同（第809-819条）",
            "【民法典-合同#运输合同】运输合同是承运人将旅客或者货物从起运地点运输到约定地点。"))
        query = "民法典第1062条内容"
        scores = {name: rerank_score(query, h)
                  for name, h in [("body", body), ("citation", citation), ("noise", noise)]}
        assert scores["body"] > scores["citation"] > scores["noise"]

    def test_hyde_rescues_rate_query(self, shared_index):
        """回归锚点：目标块不在hash直查top20召回内——靠HyDE改写把它救回来
        （这正是自适应栈相对单路直查的增量价值，评估表已量化）。"""
        from app.agent.rag.retriever import build_retriever
        adaptive = build_retriever(Settings(mock_mode=True))
        hits = adaptive.search("民间借贷利率上限是多少", top_k=3)
        assert any(h.chunk.section.startswith("借款合同") for h in hits)


# ================= Self-RAG 门与预算 =================

class TestGateAndBudget:
    def test_relevance_ratio(self):
        assert relevance("借款利率上限", "借款的利率不得违反") > 0
        assert relevance("量子纠缠", "借款合同是借款人向贷款人借款") == 0.0

    def test_out_of_corpus_reports_irrelevant(self, shared_index):
        """语料外问题：两轮策略后显式 relevant=False（承认失败而非硬答）。"""
        from app.agent.rag.retriever import build_retriever
        r = build_retriever(Settings(mock_mode=True))
        r.search("夸克颜色荷是什么", top_k=3)
        assert r.last_stats.relevant is False
        assert r.last_stats.retried is True

    def test_budget_caps(self, shared_index):
        from app.agent.rag.retriever import build_retriever
        r = build_retriever(Settings(mock_mode=True))
        r.search("离婚时房子和孩子怎么分", top_k=3)
        assert r.last_stats.searches <= MAX_SEARCHES
        assert r.last_stats.llm_calls <= MAX_LLM_CALLS


# ================= 端到端：对比护栏（docs/04 的规则落成测试） =================

class TestComparisonGuardrail:
    def _metrics(self, retriever, entries, mode):
        hit = 0
        for e in entries:
            if mode == "baseline":
                hits = retriever.base.search(e["query"], top_k=3)
            else:
                hits = retriever.search(e["query"], top_k=3)
            sources = [f"{h.chunk.doc}#{h.chunk.section}" for h in hits]
            hit += 1 if any(s in e["expected"] for s in sources) else 0
        return hit / len(entries)

    def test_adaptive_not_worse_than_baseline(self, shared_index):
        """护栏：自适应 hit@3 不得低于直查基线（违反则应收敛路由表）。"""
        from app.agent.rag.retriever import build_retriever
        r = build_retriever(Settings(mock_mode=True))
        entries = json.loads(open("app/evaluation/corpus_groundtruth.json",
                                  encoding="utf-8").read())["entries"]
        base = self._metrics(r, entries, "baseline")
        adaptive = self._metrics(r, entries, "adaptive")
        assert adaptive >= base, f"护栏违规: adaptive={adaptive} < baseline={base}"

    def test_tool_reports_strategy(self, agent_settings):
        """search_knowledge 结果透传策略与相关性标记（可观测）。"""
        from app.agent.chat import LegalAgent
        agent = LegalAgent(agent_settings)
        try:
            out_json = agent.tools.execute(
                "search_knowledge", {"query": "民间借贷利率上限是多少"})
            data = json.loads(out_json)
            assert data["success"] and "strategy" in data
            assert isinstance(data["relevant"], bool)
        finally:
            agent.close()
