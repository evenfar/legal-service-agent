"""类案检索工具（指导性案例索引）。"""

from __future__ import annotations

from app.agent.tools.mock_data import PRECEDENTS


def search_precedents(query: str) -> dict:
    """按主题词匹配指导性案例；无命中时显式说明并返回全部主题清单——
    类案检索的"没找到"必须显式，防止模型把未命中当成"没有类似案例"。"""
    hits = []
    for p in PRECEDENTS:
        haystack = p["title"] + p["topic"] + p["point"] + p["law"]
        if query and query in haystack:
            hits.append(p)
    if hits:
        return {"success": True, "query": query,
                "precedents": hits[:4],
                "note": "指导性案例裁判要点具有参照效力，个案有差异仅供参考"}
    return {"success": True, "query": query, "precedents": [],
            "topics": sorted({p["topic"] for p in PRECEDENTS}),
            "note": "案例索引未命中该主题，不代表无类似案例；"
                    "可用 search_knowledge 检索完整案例库"}
