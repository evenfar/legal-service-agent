"""规则 NLI（句级事实核验，事中防幻觉的离线版）。

与 citation.py 的分工：citation 管"引用的来源是否存在"（防编来源），
本模块管"回答内容是否被检索依据支持"（防编事实）。离线用 bigram 蕴含
近似（句子 token 被依据覆盖 ≥ 阈值 视为支持）；真实模式可升级
cross-encoder/bge-nli 或 LLM purpose="nli"（接口不变）。

保守接线：支持率过低只降置信+转人工标记（tracer 记录），不改写回复文本
——避免核验器自身的错误污染正确答案（防御性设计的对称性）。
"""

from __future__ import annotations

import re

from app.agent.rag.embedder import _tokens

SUPPORT_THRESHOLD = 0.3     # 单句：被依据覆盖的 token 比例
MIN_SENTENCE_CHARS = 8      # 过短的句（客套/过渡）不参与核验

_SENT_SPLIT = re.compile(r"[。！？!?\n；;]+")


def split_sentences(reply: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(reply)
            if s.strip() and len(s.strip()) >= MIN_SENTENCE_CHARS]


def sentence_support(sentence: str, evidence_tokens: set[str]) -> float:
    toks = _tokens(sentence)
    if not toks:
        return 1.0
    return sum(1 for t in toks if t in evidence_tokens) / len(toks)


def verify_reply_support(reply: str, evidence: list[str]) -> dict:
    """返回 {supported_ratio, unsupported: [...], checked: n}。"""
    ev_tokens: set[str] = set()
    for e in evidence:
        ev_tokens.update(_tokens(e))
    sents = split_sentences(reply)
    if not sents or not ev_tokens:
        return {"supported_ratio": 1.0, "unsupported": [], "checked": 0}
    unsupported = [s for s in sents
                   if sentence_support(s, ev_tokens) < SUPPORT_THRESHOLD]
    ratio = 1 - len(unsupported) / len(sents)
    return {"supported_ratio": round(ratio, 3), "unsupported": unsupported,
            "checked": len(sents)}
