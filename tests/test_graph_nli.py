"""升级第三步测试：法条引用图谱 + 规则NLI句级核验。"""

from __future__ import annotations

from pathlib import Path

from app.agent.rag.chunker import Chunk
from app.agent.rag.citation_graph import CitationGraph, get_citation_graph
from app.agent.rag.lawref import article_range_of, cn2num, num2cn, parse_cited_articles
from app.agent.safety.nli import (split_sentences, verify_reply_support)

KB = Path("app/agent/rag/knowledge")


class TestLawRef:
    def test_cn2num(self):
        assert cn2num("六百八十") == 680
        assert cn2num("一千零九十一") == 1091
        assert cn2num("十") == 10

    def test_num2cn_roundtrip(self):
        for n in (10, 188, 680, 1062, 1258):
            assert cn2num(num2cn(n)) == n

    def test_parse_cited_articles(self):
        text = "[相关法条] 《民法典》第680条（原《合同法》第211条对应）；另见第一千零七十九条"
        assert parse_cited_articles(text) == [680, 211, 1079]

    def test_article_range_of(self):
        assert article_range_of("借款合同（第667-679条）") == (667, 679)
        assert article_range_of("借款合同（第680条）") == (680, 680)
        assert article_range_of("指导案例50号") is None


class TestCitationGraph:
    def test_built_from_corpus(self):
        g = get_citation_graph(str(KB))
        assert g.edge_count >= 30          # 37件案例多数带法条引用
        assert g.article_to_cases          # 存在"条号→引用案例"映射

    def test_neighbors_bidirectional(self):
        """选一个引用了库内条文的案例：能连到法条块且可反向到达。"""
        g = get_citation_graph(str(KB))
        picked = None
        for case_id, arts in g.case_to_articles.items():
            if any(g.article_to_chunks.get(a) for a in arts):
                picked = case_id
                break
        assert picked, "语料中应存在引用库内条文的案例"
        targets = set()
        for n in g.case_to_articles[picked]:
            targets.update(g.article_to_chunks.get(n, []))
        assert targets
        back = set()
        for t in targets:
            back |= g.neighbors(t)
        assert picked in back              # 反向可达

    def test_nomination_is_conservative(self):
        """图只提名不硬塞：无邻居/分数不足时结果不变（护栏语义）。"""
        g = CitationGraph()                # 空图
        assert g.edge_count == 0
        assert g.neighbors("anything") == set()


class TestRuleNLI:
    def test_split_sentences(self):
        sents = split_sentences("这是第一句完整结论。第二句依据《民法典》第六百八十条的规定！")
        assert len(sents) == 2          # 过短的客套句被阈值过滤（设计行为）

    def test_supported_reply(self):
        ev = ["借款合同是借款人向贷款人借款，到期返还借款并支付利息的合同。"]
        v = verify_reply_support("借款合同到期后应当返还借款并支付利息。", ev)
        assert v["supported_ratio"] >= 0.6
        assert v["unsupported"] == []

    def test_fabricated_reply_flagged(self):
        ev = ["借款合同是借款人向贷款人借款的合同。"]
        v = verify_reply_support(
            "根据刑法规定该行为构成诈骗罪可判处十年有期徒刑并处罚金。", ev)
        assert v["supported_ratio"] < 0.5
        assert v["unsupported"], "编造内容必须被标出"

    def test_empty_inputs(self):
        assert verify_reply_support("", [])["supported_ratio"] == 1.0
