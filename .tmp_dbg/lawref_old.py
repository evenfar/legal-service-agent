"""条号解析工具：中文/阿拉伯条号互转、引用行解析、区间标签解析。

集中放在一处（law.py 工具与 citation_graph 共用），避免两份转换逻辑漂移。
"""

from __future__ import annotations

import re

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def cn2num(cn: str) -> int:
    result = num = 0
    for ch in cn:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch == "千":
            result += (num or 1) * 1000
            num = 0
        elif ch == "百":
            result += (num or 1) * 100
            num = 0
        elif ch == "十":
            result += (num or 1) * 10
            num = 0
    return result + num


def num2cn(n: int) -> str:
    if n < 10:
        return "零一二三四五六七八九"[n]
    if n >= 1000:
        thousand, rest = n // 1000, n % 1000
        cn = num2cn(thousand) + "千"
        if rest:
            cn += ("零" + num2cn(rest)) if rest < 100 else num2cn(rest)
        return cn
    if n < 100:
        if n < 20:
            return "十" + (num2cn(n % 10) if n % 10 else "")
        return num2cn(n // 10) + "十" + (num2cn(n % 10) if n % 10 else "")
    h, rest = n // 100, n % 100
    head = num2cn(h) + "百"
    if rest == 0:
        return head
    if rest < 10:
        return head + "零" + num2cn(rest)
    if rest < 20:
        return head + "零" + num2cn(rest)
    return head + num2cn(rest)


_CITE_RE = re.compile(r"第([一二三四五六七八九十百千零]+|\d+)条")


def parse_cited_articles(text: str) -> list[int]:
    """解析文本中引用的全部条号（如 [相关法条] 行的《民法典》第X条）。"""
    out: list[int] = []
    for m in _CITE_RE.finditer(text):
        tok = m.group(1)
        n = int(tok) if tok.isdigit() else cn2num(tok)
        if 1 <= n <= 1260:
            out.append(n)
    return list(dict.fromkeys(out))


def article_range_of(section: str) -> tuple[int, int] | None:
    """'借款合同（第667-679条）' → (667, 679)；'（第680条）' → (680, 680)。"""
    m = re.search(r"第(\d+)-(\d+)条", section)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"第(\d+)条", section)
    if m:
        n = int(m.group(1))
        return n, n
    return None
