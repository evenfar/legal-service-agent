# 第02篇 · 语料管线 ingest：从民法典官方文本到向量索引

> 📍 学习路径：[README入口](../README.md) → [01域适配](01-法律域适配与复用架构.md) → **02语料管线** → [05API](05-API服务与前端.md) → [03评估安全](03-评估与安全.md) → [04RAG机制](04-Phase2-RAG机制栈路线图.md) → [06调试实录](06-调试实录.md) → [链路浏览器](chain-explorer.html)

> 一句话目标：把 `raw/minfadian_full.txt`（三编 700 条官方公开文本）加工成"检索粒度合适、条号可回溯、结构可校验"的知识库 md，再建向量索引——含一个真实调试案例：章边界丢条 bug 的完整复盘。

## 本期目标

- 跑通并读懂 `python -m app.scripts.ingest_laws`：解析（编/章/条）→ 同章聚合切块 → 生成 md → 三重校验 → 重建索引。
- 理解法律文本切块的两难（单条太碎/整章太粗）与"同章贪心聚合 + 条号区间标签"的解法。
- 复盘章边界丢条 bug：43 条条文如何在解析阶段被静默吞掉、为什么守恒校验抓不到、怎么修。
- 弄清两个方向的中文数字转换（_cn2num / _num2cn）各自服务什么环节。
- 理解 groundtruth 标注与 Phase 2 检索评估（hit-rate/MRR）的关系。

## 背景与数据流

```
【离线一次：语料管线】 app/scripts/ingest_laws.py
  raw/minfadian_full.txt（官方文本 700 条 = 合同526 + 婚姻家庭79 + 侵权责任95）
      │ parse_minfadian()：编/章/条状态机逐行扫描
      ▼ list[Article(no, book, chapter, text)]        ← no 如"第四百六十三条"
  aggregate_articles()：同章贪心聚合，达到 900 字封块，绝不跨章
      ▼ list[Chunk(title, book, chapter, articles)]   ← 94 块，平均 657 字
      │   title 在这里补上条号区间："合同的订立（第469-480条）"
  write_law_md()：按编分组写 3 个 md（H2=聚合块，头部 DISCLAIMER）
      ▼ app/agent/rag/knowledge/民法典-{合同,婚姻家庭,侵权责任}.md（H2 数 68/11/15）
  validate()：三重校验（条文守恒 700 / 免责声明 / H2 结构），失败 SystemExit(1)
      ▼
  build_index() (app/agent/rag/retriever.py，复用 med)
      │ chunk_markdown_dir() 按 ## 切片 → embedder.encode → backend.upsert
      ▼ app/sessions/kb_index.json（embedding 模型名一并写入）
  自检：retriever.search("民间借贷利率上限", top_k=2) 打印命中 来源#小节
【每次提问】search_knowledge (app/agent/tools/knowledge.py) → KnowledgeRetriever.search
```

## 核心实现讲解

### 1. 条文解析：编/章/条状态机（parse_minfadian）

逐行扫描，三个正则分派：编 `_BOOK_RE`（"第三编 合同"）、章/分编 `_CHAPTER_RE`（"第二章 合同的订立"/"第二分编 典型合同"）、条 `_ARTICLE_RE`（`^第[一二三四五六七八九十百千零]+条`）。状态只有两个变量：当前 `book/chapter`（边界行更新）与 `current: Article | None`（条文行创建，续行 `current.text += "\n" + line` 拼接多款条文）。**核心纪律：只有遇到"下一条条文"才把 current 收进 articles**——所以每类边界行必须先把 pending 的 current 收尾（这正是 bug 的来源，见下节）。章名缺失时兜底"总则性规定"；文件末尾再收尾一次。

### 2. 章边界丢条 bug 复盘（本项目最有教学价值的调试案例）

**现象**：管线首跑打印"解析条文 657 条"，而民法典三编的官方条数是公开事实：700。

**最小复现**（边界分支把 pending 条文当作"上一章的脏状态"清掉）：

```python
for line in lines:
    if 编.match(line):
        current = None                       # ← bug：未入库的条文被当脏状态丢弃
        book = ...; continue
    if 章.match(line):
        current = None                       # ← 同上
        chapter = ...; continue
    if 条.match(line):
        if current: articles.append(current) # 只有遇到下一条才收尾上一条
        current = Article(...)
```

取语料头部做输入（编标题 → 第一分编 → 第一章 一般规定 → 第463~468 条 →
"第二章 合同的订立" → 第四百六十九条），修复前输出 6 条：463~467 **加 469**，
恰好缺第四百六十八条——它是第一章最后一条，处于 pending 状态时撞上第二章标题行
被 `current = None` 清掉，469 正常入库掩盖了缺口。

**全量影响（脚本实测）**：语料共 50 个边界（3 编 + 47 章/分编），其中 43 个边界
撞上时存在 pending 条文 → 丢失 43 条，解析得 657 条（合同 497 / 婚姻家庭 74 /
侵权责任 86，即每编分别丢 29/5/9 条）。规律：**每个编/章的最后一条被吞**；
另有 7 个边界（文件头、编/分编/章标题连续出现处）当时无 pending，故未丢。

**根因**：把"章节标题行"当成纯元数据更新，顺手把未落账的正文状态 `current`
也当"上一章遗留"一并重置。状态机的每一类转移都必须处理**全部**活跃状态，
而不是只重置自己关心的那个变量。

**为什么 validate 的条文守恒抓不到它**：守恒校验比的是"解析条数 vs 写入 md 条数"——
两者同源（都来自 articles 列表），解析端丢的条，md 端同样没有，守恒依然成立。
**同源校验只能抓"解析→写入"环节的丢失，抓不了"解析→解析"环节的丢失**。

**真正抓住它的是外部锚点**：`tests/test_legal_modules.py::test_parse_all_700_articles`
用官方事实做断言（700 条，且每编精确到 526/79/95）；`main()` 首行的条数打印
也是锚点。补充手段是**条号连续性检查**：丢条处条号序列出现跳跃（…467, 469…）。

**修复**（ingest_laws.py 现状）：每个边界分支先收尾再更新状态：

```python
if m:  # 编边界
    if current:            # 先收尾当前条文（否则每编/章的最后一条被丢）
        articles.append(current)
        current = None
    book = m.group(1).strip()
    continue
```

章/分编边界同样处理。修复后 700 条，`test_parse_all_700_articles` 全绿。

### 3. 同章主题聚合：900 字贪心、绝不跨章（aggregate_articles）

- 逐条累加进当前 Chunk，触发封块的三条件是**或**关系：换编、换章、`current.size + 本条长度 > MAX_CHUNK_CHARS(900)`。绝不跨章保证块内主题纯净（"买卖合同"与"赠与合同"永不同块）。
- 封块后统一补条号区间标签：`_cn2num` 把首末条号转阿拉伯数字，`f"{chapter}（第{first}-{last}条）"`——区间天然唯一（如"借款合同（第680条）"），检索命中可精确回溯条文，也是引用溯源的锚点。
- **900 与 chunker 的 1200 的关系**：`app/agent/rag/chunker.py` 也有一个 `MAX_CHUNK_CHARS=1200`（同名不同值！），聚合块要再过一遍 H2 切片 + 二次打包，900 的目标值 + "最后一条允许超额"意味着块大小上限约 1100（测试断言 `c.size <= 1100`），给 `【doc#section】` 前缀留余量后仍 < 1200——**聚合块与向量 chunk 基本保持 1:1**，条号区间标签不会被二次切散。
- `MIN_CHUNK_CHARS = 80` 已声明但实际未参与逻辑：因为"绝不跨章"已保证孤立短章必然独立成块，这个常量是预留语义（见练习 2）。
- 实测结果：94 块（合同 68 / 婚姻家庭 11 / 侵权责任 15），平均 657 字——单条平均仅百余字，不聚合则检索粒度过碎、上下文过散。

### 4. 中文数字转换：两个方向、两个文件

| 函数 | 位置 | 方向 | 用途 |
|---|---|---|---|
| `_cn2num(cn)` | `app/scripts/ingest_laws.py` | 中文→阿拉伯 | 聚合块标题生成条号区间（"四百六十三"→463） |
| `_num2cn(n)` / `_normalize_article(raw)` | `app/agent/tools/law.py` | 阿拉伯→中文 | 工具直查时把"第1062条"归一化成"第一千零六十二条"再查 LAW_ARTICLES |

两者都只需覆盖民法典条号范围（1~1260），所以 `_cn2num` 用"分段累加"就够
（遇 千/百/十 把 `(num or 1) * 位值` 累进 result，`num or 1` 处理"十"“千"前省略 1 的写法）；
`_num2cn` 递归处理 100~999 段并按需补"零"（一千零一）。**用测试锚定边界**：
`test_cn2num` 覆盖 463/1001/1258/10。不引第三方库（cn2an）是刻意的：百行内的
确定性纯函数比依赖更容易被审计——语料管线的每一步都应可复算。

### 5. 三重校验（validate）：让坏数据在入库前死掉

1. **条文守恒**：md 中 `^第…条` 行数 == 解析条数（700）。抓写入环节丢失/重复。
2. **免责声明**：每个 md 必含 `DISCLAIMER[:20]` 片段——合规底线："科普级程序指引，
   不构成法律意见"必须随每份语料落地，检索结果单看一个 chunk 也带声明语境。
3. **H2 结构**：`^## ` 至少一个——没有 H2 的 md 会被 chunker 整篇降级成"概览"单块，
   检索粒度崩坏。这是"上游数据形状约束下游切片"的契约式校验。

任一失败 `SystemExit(1)`，且 `--report-only` 支持只校验不重建索引。

### 6. 类案例入库与 groundtruth：为 Phase 2 评估备粮

- `ingest_precedents()`：解析 `raw/precedents_raw.md`（`## 指导案例N号：标题` 结构，语料就绪时），每案例一块写入 `指导案例选编.md`；`--skip-precedents` 可跳过。
- `app/evaluation/corpus_groundtruth.json`：12 条"query → 应命中的 文档#小节"标注
  （如 `"民间借贷利率上限是多少" → ["民法典-合同#借款合同（第680条）"]`），expected
  与实际生成的 H2 标题逐字对齐（`test_groundtruth_file_valid` 校验文档存在且非空）。
- **与 Phase 2 的关系**：当前离线 HashEmbedder 只保证链路可跑，不保证检索质量；
  Phase 2 接真实 embedding + AdaptiveRetriever（HyDE/Rerank 等）时，这 12 条真值就是
  hit-rate / MRR 的评分基准——**先标真值再调检索**，否则优化没有裁判。
- 别忘了 `build_retriever()`（retriever.py）的离线兜底：索引缺失时自动用
  HashEmbedder 建索引保证开箱即跑；索引里记录的 embedding 模型名在 `load()` 时
  与当前 embedder 比对，不一致直接 ValueError——换了 embedding 必须重建索引。

## 知识点与面试考点

1. **法律文本切块为什么不能按固定字数滑窗？** 要点：条文是天然最小单元但过碎（几十~两百字）；滑窗会把一条条文腰斩、把无关章节混入同一块；同章贪心聚合 + 绝不跨章 + 条号区间标签，粒度与可回溯性兼得。
2. **状态机解析器的经典陷阱是什么？** 要点：某类转移只更新自己关心的状态、忽略了其他活跃状态（pending 的 current）——每章最后一条被吞 43 条；每类边界转移都要收尾全部状态。
3. **守恒校验为什么抓不到解析端的丢失？** 要点：校验双方同源（都出自 articles 列表），只能对比"下游 vs 上游快照"，抓不了上游自身的错；必须引入外部锚点（官方总条数、逐编精确数）或不变量（条号连续性）。
4. **数据管线的校验应该放在哪一层？** 要点：入库前 fail-fast（validate 三重校验 + SystemExit），而不是等检索质量劣化后再回头查语料；沉默的数据损坏是最贵的 bug。
5. **两个模块都有 MAX_CHUNK_CHARS 但值不同（900/1200），是坏味道吗？** 要点：ingest 的 900 是聚合目标、chunker 的 1200 是二次打包上限，前者刻意小于后者留余量，保证聚合块与向量 chunk 1:1；但同名常量确实易误读，命名上应区分（AGGREGATE_TARGET vs REPACK_CAP）。
6. **为什么自己写 _cn2num 而不用库？** 要点：域内范围（1~1260）内百行纯函数可审计、可测试锚定（463/1001/1258）；语料管线每步可复算是可信度的来源。
7. **groundtruth 在 RAG 迭代中扮演什么角色？** 要点：检索优化（换 embedding/HyDE/Rerank）没有真值就没有裁判；12 条标注在语料定稿时同步生成并测试锁定，Phase 2 直接用 hit-rate/MRR 打分。
8. **检索结果为什么必须带条号区间标签？** 要点：`source = doc#section` 是引用溯源（validate_citations/append_sources_if_missing）的锚点；"借款合同（第680条）"让用户与安全层都能核验"这条回答出自哪条法律"。

## 动手练习

1. **亲手复现 bug**：复制 parse_minfadian，把两个边界分支的收尾三行改成 `current = None`，对 `raw/minfadian_full.txt` 跑一遍并按编统计条数——验证你得到 657（合同 497/婚姻家庭 74/侵权责任 86）；再写一个"条号连续性检查"函数（同编内条号差恒为 1），证明它能独立抓出全部 43 个丢失点。
2. **实现 MIN_CHUNK_CHARS 的语义**：让过短的孤立章（如只有 1~2 条的章）在"邻块同编且合并后不超上限"时并入前一块（仍绝不跨章）；先想清楚这个改动会不会破坏"条号区间标签唯一性"，再用 `test_aggregate_never_crosses_chapter` 扩展断言。
3. **给管线加锚点**：在 `validate()` 里新增第四重校验——"每编首条条号与官方一致"（合同 463/婚姻家庭 1040/侵权责任 1164）与"全库条号无缺口"；构造一个删掉中间 10 条的 md，确认校验能抓住而条文守恒抓不住（守恒两侧同删）。
