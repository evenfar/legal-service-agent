"""法律语料入库管线（本项目核心新组件）。

流程：raw/ 原始文本 → 解析（编/章/条）→ 主题聚合切块 → 生成 knowledge/*.md
→ 结构校验 → 重建向量索引 → 报告。

法律文本切块的真实难题与解法（教学核心）：
- 条文是天然最小单元但普遍过碎（几十~两百字），单条成块检索粒度太细；
- 直接按编/章整章成块又太粗（一章可达万字）；
- 本管线的策略：**同章内条文贪心聚合**——按条文顺序向当前块累加，
  达到目标字数（900）即封块，绝不跨章，块标签携带条号区间
  【民法典·合同编#合同的订立（第469-494条）】—— 检索命中可精确回溯条文。

用法：python -m app.scripts.ingest_laws [--skip-precedents] [--report-only]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

RAW_DIR = ROOT / "raw"
KB_DIR = ROOT / "app" / "agent" / "rag" / "knowledge"
MAX_CHUNK_CHARS = 900          # 聚合块目标上限（与 chunker 的 1200 上限兼容）
MIN_CHUNK_CHARS = 80           # 过短的孤立章也独立成块（保留结构）

_ARTICLE_RE = re.compile(r"^第[一二三四五六七八九十百千零]+条")
_BOOK_RE = re.compile(r"^第[一二三四五六七八九十]+编\s*(.+)")
_CHAPTER_RE = re.compile(r"^(第[一二三四五六七八九十百千零]+分?编|第[一二三四五六七八九十百千零]+章)\s*(.*)")

DISCLAIMER = "> 本知识库内容为《中华人民共和国民法典》（2021年1月1日施行版）及最高人民法院" \
             "公开发布的指导性案例，属公有领域法律文本。检索结果仅供学习参考，不构成法律意见。"


@dataclass
class Article:
    no: str            # 第四百六十三条
    book: str          # 合同
    chapter: str       # 一般规定
    text: str


@dataclass
class Chunk:
    title: str         # 合同的订立（第469-474条）
    book: str
    chapter: str = ""  # 聚合依据（章名，不含条号后缀）
    articles: list[Article] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n\n".join(f"{a.no} {a.text}" for a in self.articles)

    @property
    def size(self) -> int:
        return sum(len(a.no) + len(a.text) + 1 for a in self.articles)


def parse_minfadian(path: Path) -> list[Article]:
    articles: list[Article] = []
    book = chapter = ""
    current: Article | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^第[一二三四五六七八九十]+编\s+(.+)$", line)
        if m:
            if current:  # 编边界：先收尾当前条文（否则每编/章的最后一条被丢）
                articles.append(current)
                current = None
            book = m.group(1).strip()
            continue
        m = re.match(r"^(第[一二三四五六七八九十百千零]+分编|第[一二三四五六七八九十百千零]+章)\s*(.*)$", line)
        if m:
            if current:  # 章边界：同样先收尾
                articles.append(current)
                current = None
            if m.group(2).strip():
                chapter = m.group(2).strip()
            continue
        if re.match(r"^第[一二三四五六七八九十百千零]+节\s*", line):
            if current:   # 节标题独立：不混入上一条正文（审查#25）
                articles.append(current)
                current = None
            continue
        if _ARTICLE_RE.match(line):
            if current:
                articles.append(current)
            no, _, text = line.partition(" ")
            current = Article(no=no, book=book, chapter=chapter or "总则性规定",
                              text=text.strip())
        elif current:
            current.text += "\n" + line
    if current:
        articles.append(current)
    return articles


def aggregate_articles(articles: list[Article]) -> list[Chunk]:
    """同章贪心聚合：按条序累加至 MAX_CHUNK_CHARS 封块，绝不跨章。"""
    chunks: list[Chunk] = []
    current: Chunk | None = None
    for art in articles:
        if current and (current.book != art.book or current.chapter != art.chapter
                        or current.size + len(art.no) + len(art.text) > MAX_CHUNK_CHARS):
            chunks.append(current)
            current = None
        if current is None:
            current = Chunk(title=art.chapter, book=art.book, chapter=art.chapter,
                            articles=[])
        current.articles.append(art)
    if current:
        chunks.append(current)
    # 标题携带条号区间（区间本身保证唯一，无需额外序号）
    for c in chunks:
        first = _cn2num(c.articles[0].no.replace("第", "").replace("条", ""))
        last = _cn2num(c.articles[-1].no.replace("第", "").replace("条", ""))
        rng = first if first == last else f"{first}-{last}"
        c.title = f"{c.title}（第{rng}条）"
    return chunks


_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9}


def _cn2num(cn: str) -> int:
    """中文数字（民法典范围 1~1260）转阿拉伯数字，分段累加。"""
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


def write_law_md(chunks: list[Chunk]) -> dict[str, int]:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    by_book: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_book.setdefault(c.book, []).append(c)
    stats = {}
    for book, book_chunks in by_book.items():
        lines = [f"# 民法典·{book}", "", DISCLAIMER, ""]
        for c in book_chunks:
            lines.append(f"## {c.title}")
            lines.append("")
            lines.append(c.body)
            lines.append("")
        path = KB_DIR / f"民法典-{book}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        stats[path.name] = len(book_chunks)
    return stats


def ingest_precedents() -> dict:
    """解析指导案例 md（## 指导案例N号：标题 结构），每案例一块。"""
    src = RAW_DIR / "precedents_raw.md"
    if not src.exists():
        return {"skipped": "raw/precedents_raw.md 不存在"}
    text = src.read_text(encoding="utf-8")
    sections = re.split(r"^## ", text, flags=re.MULTILINE)
    count = 0
    lines_out = ["# 最高人民法院指导性案例（民事选编）", "", DISCLAIMER, ""]
    for sec in sections[1:]:
        sec = sec.strip()
        if not sec:
            continue
        if not sec.startswith("指导案例"):   # 来源附录等非案例标题不计（审查#25）
            continue
        lines_out.append(f"## {sec}")
        lines_out.append("")
        count += 1
    if count:
        (KB_DIR / "指导案例选编.md").write_text("\n".join(lines_out), encoding="utf-8")
    return {"precedents": count}


def validate(articles: list[Article], md_stats: dict) -> list[str]:
    errors: list[str] = []
    # 条文守恒：md 中条数 == 解析条数
    total_in_md = 0
    for md in KB_DIR.glob("民法典-*.md"):
        total_in_md += sum(1 for line in md.read_text(encoding="utf-8").splitlines()
                           if _ARTICLE_RE.match(line))
    if total_in_md != len(articles):
        errors.append(f"条文数不守恒: 解析{len(articles)} vs 写入md{total_in_md}")
    for md in KB_DIR.glob("*.md"):
        content = md.read_text(encoding="utf-8")
        if DISCLAIMER[:20] not in content:
            errors.append(f"{md.name} 缺免责声明")
        if not re.search(r"^## ", content, re.MULTILINE):
            errors.append(f"{md.name} 无二级标题（chunker无法切分）")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="法律语料入库管线")
    parser.add_argument("--skip-precedents", action="store_true")
    parser.add_argument("--report-only", action="store_true",
                        help="只校验与报告，不重建索引")
    args = parser.parse_args()

    src = RAW_DIR / "minfadian_full.txt"
    if not src.exists():
        raise SystemExit(f"缺少原始语料 {src}，请先放入 raw/ 目录")
    articles = parse_minfadian(src)
    books = {}
    for a in articles:
        books[a.book] = books.get(a.book, 0) + 1
    print(f"解析条文 {len(articles)} 条：{books}")

    chunks = aggregate_articles(articles)
    md_stats = write_law_md(chunks)
    prec = {"skipped": "flag"} if args.skip_precedents else ingest_precedents()
    print(f"生成 md：{md_stats}｜指导案例：{prec}")

    errors = validate(articles, md_stats)
    if errors:
        for e in errors:
            print(f"❌ 校验失败: {e}")
        raise SystemExit(1)
    print("✅ 校验通过：条文守恒 / 免责声明 / H2 结构")

    if not args.report_only:
        from app.agent.rag.retriever import build_index
        from app.config.settings import Settings
        settings = Settings(mock_mode=True)
        retriever, n = build_index(settings)
        print(f"✅ 索引重建完成：{n} chunks，embedding={retriever.embedder_model}")
        demo = retriever.search("民间借贷利率上限", top_k=2)
        print("   自检: 检索「民间借贷利率上限」→",
              [f"{c.chunk.doc}#{c.chunk.section}" for c in demo])


if __name__ == "__main__":
    main()
