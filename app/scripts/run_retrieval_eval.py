"""检索质量对比评估（Phase 2）：同题对比 直查基线 / +Rerank / 自适应组合。

指标：hit-rate@3（top-3 是否含标注小节）与 MRR（首个命中倒数排名）。
命中判定：{doc}#{section} 精确匹配 groundtruth 标注。
护栏断言（--strict）：自适应组合 hit-rate 不得低于直查基线，
否则按 docs/04 的规则应从路由表摘除对应策略。

用法：python -m app.scripts.run_retrieval_eval [--strict]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.rag.adaptive import (AdaptiveRetriever, RECALL_K,  # noqa: E402
                                    rerank_score)
from app.agent.rag.retriever import build_retriever  # noqa: E402
from app.config.settings import Settings  # noqa: E402


def load_groundtruth() -> list[dict]:
    data = json.loads((ROOT / "app/evaluation/corpus_groundtruth.json")
                      .read_text(encoding="utf-8"))
    return data["entries"]


def hit_metrics(hits, expected: list[str]) -> tuple[float, float, str]:
    """返回 (hit@k, RR, 首个命中source)。"""
    sources = [f"{h.chunk.doc}#{h.chunk.section}" for h in hits]
    for rank, src in enumerate(sources, start=1):
        if src in expected:
            return 1.0, 1.0 / rank, src
    return 0.0, 0.0, ""


def evaluate(retriever, entries, mode: str) -> dict:
    hit = mrr = 0.0
    llm = searches = 0
    rows = []
    for e in entries:
        if mode == "baseline":
            hits = retriever.base.search(e["query"], top_k=3)
            n_llm = n_search = 1
        elif mode == "rerank":
            recalled = retriever.base.search(e["query"], top_k=RECALL_K)
            hits = sorted(recalled, key=lambda h: -rerank_score(e["query"], h))[:3]
            n_llm, n_search = 0, 1
        elif mode == "hybrid":
            from app.agent.rag.bm25 import get_bm25
            from app.agent.rag.adaptive import rrf_merge
            from app.agent.rag.backends.base import RetrievedChunk
            from pathlib import Path as _P
            dense = retriever.base.search(e["query"], top_k=RECALL_K)
            from app.agent.rag.adaptive import DENSE_GATE
            if dense and dense[0].score >= DENSE_GATE:
                bm = []          # 门控：语义已强，BM25不并票
            else:
                bm = get_bm25(str(_P("app/agent/rag/knowledge"))).search(e["query"], RECALL_K)
            pool = {h.chunk.chunk_id: h for h in dense}
            idx = get_bm25(str(_P("app/agent/rag/knowledge")))
            top = bm[0][1] if bm else 1.0
            for cid, sc in bm:
                if cid not in pool:
                    ch = idx.get_chunk(cid)
                    if ch is not None:
                        pool[cid] = RetrievedChunk(chunk=ch, score=sc / (top or 1.0))
            fused = rrf_merge([[h.chunk.chunk_id for h in dense], [c for c, _ in bm]])
            hits = sorted(pool.values(),
                          key=lambda h: -fused.get(h.chunk.chunk_id, 0.0))[:RECALL_K]
            hits = sorted(hits, key=lambda h: -rerank_score(e["query"], h))[:3]
            n_llm, n_search = 0, 1
        else:  # adaptive 全栈
            hits = retriever.search(e["query"], top_k=3)
            st = retriever.last_stats
            n_llm, n_search = st.llm_calls, st.searches
        h, rr, src = hit_metrics(hits, e["expected"])
        hit += h
        mrr += rr
        llm += n_llm
        searches += n_search
        rows.append((e["query"][:16], h, rr, src[:34] if src else "✗"))
    n = len(entries)
    return {"hit": hit / n, "mrr": mrr / n, "llm": llm / n,
            "searches": searches / n, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="护栏：自适应 hit-rate 不得低于基线")
    parser.add_argument("--real", action="store_true",
                        help="真实embedding列（读.env配置与真实索引）")
    args = parser.parse_args()

    entries = load_groundtruth()
    offline = not args.real
    settings = Settings(mock_mode=offline)
    if not offline:
        settings.kb_index_path = "app/sessions/kb_index_real.json"
    retriever = build_retriever(settings)  # AdaptiveRetriever

    results = {m: evaluate(retriever, entries, m)
               for m in ("baseline", "rerank", "hybrid", "adaptive")}

    emb = "真实 text-embedding-3-small" if not offline else "离线 HashEmbedder"
    header = chr(10) * 0 + "═══ 检索质量对比（{} 条 groundtruth，{}）═══".format(len(entries), emb)
    print()
    print(header)
    print(f"{'机制':<12}{'hit@3':>8}{'MRR':>8}{'平均LLM':>9}{'平均检索':>9}")
    for name, r in results.items():
        print(f"{name:<12}{r['hit']:>8.2f}{r['mrr']:>8.3f}"
              f"{r['llm']:>9.1f}{r['searches']:>9.1f}")

    print("\n── 自适应逐条 ──")
    for q, h, rr, src in results["adaptive"]["rows"]:
        print(f"  {'✓' if h else '✗'} {q:<18} RR={rr:.2f} {src}")

    if args.strict and results["adaptive"]["hit"] < results["baseline"]["hit"]:
        print("\n🚨 护栏违规：自适应 hit-rate 低于基线——按 docs/04 规则应收敛路由表")
        raise SystemExit(1)
    print("\n✅ 评估完成（真实 embedding 接入后重跑本表即为 Phase 2 终版）")


if __name__ == "__main__":
    main()
