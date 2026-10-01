"""法条查询工具：条号直查 或 主题词检索。

教学要点：法条是精确引用型知识 —— 单号/条号适合结构化直查（本工具），
主题理解适合向量检索（search_knowledge 走 RAG）。两层互补正是
"关键词保精确、语义保泛化"的落地。
"""

from __future__ import annotations

import re

from app.agent.tools.mock_data import LAW_ARTICLES, LAW_TOPICS

from app.agent.rag.lawref import num2cn  # 条号转换集中一处（lawref.py），消除双副本漂移


def _normalize_article(raw: str) -> str:
    m = re.match(r"第(\d+)条", raw.strip())
    if not m:
        return raw.strip()
    n = int(m.group(1))
    if 1 <= n <= 1260:
        return f"第{num2cn(n)}条"
    return raw.strip()


def query_law_article(query: str) -> dict:
    """query 可以是条号（第1062条/第一千零六十二条）或主题词（抚养权/利率）。"""
    q = query.strip()
    if q in LAW_ARTICLES:
        return {"success": True, "article": LAW_ARTICLES[q], "match_type": "条号精确匹配"}
    norm = _normalize_article(q)
    if norm in LAW_ARTICLES:
        return {"success": True, "article": LAW_ARTICLES[norm],
                "match_type": "条号归一化匹配"}
    # 主题词最长匹配（"家暴赔偿"优先于"赔偿"）
    for topic in sorted(LAW_TOPICS, key=len, reverse=True):
        if topic in q:
            art = LAW_ARTICLES[LAW_TOPICS[topic]]
            return {"success": True, "article": art,
                    "match_type": f"主题词「{topic}」匹配",
                    "hint": "更完整的条文语境请用 search_knowledge 检索知识库"}
    return {"success": False,
            "error": f"高频法条索引未收录「{q}」。可换主题词重试，或用 "
                     f"search_knowledge 检索民法典全量知识库",
            "available_topics": list(dict.fromkeys(LAW_TOPICS.values()))[:8]}
