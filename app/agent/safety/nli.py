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
    """短句免检例外：含数字/义务词的高风险句参与核验（审查#24）。"""
    out = []
    for s in _SENT_SPLIT.split(reply):
        s = s.strip()
        if not s:
            continue
        risky = bool(re.search(r"\d|应当|必须|不得|禁止|可以|有权", s))
        if len(s) >= MIN_SENTENCE_CHARS or risky:
            out.append(s)
    return out


_NEGATIONS = ("不", "未", "无", "没", "禁止", "不得", "拒绝")


def sentence_support(sentence: str, evidence_tokens: set[str]) -> float:
    """词汇支持启发式（非事实正确概率，审查#24标注）：覆盖率+否定冲突惩罚。"""
    toks = _tokens(sentence)
    if not toks:
        return 1.0
    ratio = sum(1 for t in toks if t in evidence_tokens) / len(toks)
    ev_join = " ".join(evidence_tokens)
    sent_neg = any(n in sentence for n in _NEGATIONS)
    if sent_neg and ratio > 0.5:
        key_term = next((t for t in toks if len(t) >= 2 and t in ev_join), "")
        if key_term:
            pos = ev_join.find(key_term)
            window = ev_join[max(0, pos - 8):pos + len(key_term) + 8]
            if not any(n in window for n in _NEGATIONS):
                ratio *= 0.5
    return ratio


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
