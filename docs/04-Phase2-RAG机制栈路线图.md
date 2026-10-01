> 状态：核心机制已实现（adaptive.py + run_retrieval_eval.py），实测对比表见下；本文档其余部分为设计依据存档。

# 04 Phase 2 预研：AdaptiveRetriever 机制栈路线图

> 一句话目标：在现有 `KnowledgeRetriever` 之上规划一条"路由 → 粗召回 → 精排 → 反思 → 重试"的自适应检索流水线，并用 `corpus_groundtruth` 的 12 条标注做同题对比，让每个机制"值不值得开"由数字决定。
>

## 背景与数据流

现状（已实现，`app/agent/rag/retriever.py`）：`KnowledgeRetriever.search(query, top_k)` 做单段向量检索——`embedder.encode_one(query)` → `backend.search(q_vec, top_k or 3)`，`load()` 里做 embedding 模型一致性校验防"换向量器用旧索引"。离线 `HashEmbedder`（`app/agent/rag/embedder.py`，字符 bigram + MD5 → 64 维）语义无关但跨进程稳定，所以离线只能验证"管线确定性"，验证不了"语义收益"。

```
【现状】 query ──encode_one──► backend.search(q_vec, 3) ──► top-3 chunks
                                   │
                                   ▼
              app/agent/tools/knowledge.py search_knowledge
                source = f"{chunk.doc}#{chunk.section}" → safety 引用校验

【规划】                    AdaptiveRetriever（未实现）
  query
    │ ① route(query) ──启发式正则，免费──► direct / hyde / fusion / stepback
    ▼
  ② _recall(query, strategy, top_k=20)      粗召回（改造查询或多次检索）
    ▼
  ③ _rerank(query, cands)[:3]               精排（离线=规则打分，真实=cross-encoder）
    ▼
  ④ _self_rag(query, top3)                  三问：要检索吗/相关吗/被支持吗
    │ 不达标 ──► _fallback(strategy) ──► 重试一次（回到②，预算内最多 2 策略）
    ▼
  (chunks, meta{"strategy","attempt"}) ──► search_knowledge 契约不变
```

语料事实：`app/agent/rag/knowledge/` 三个 .md 共 94 个 H2 小节（合同 68 / 婚姻家庭 11 / 侵权责任 15），ingest 阶段已做同章条文聚合（单块上限 1100 字），`tests/test_legal_modules.py` 断言索引 `size() >= 90`。

## 核心讲解

### 1. 要解决的四类检索故障（以现有 top-3 直查为基线）

① 词汇失配：口语 query 与条文正式语体字面不重叠（"老公外面有人想离婚" vs `离婚（第1076-1083条）`）；② 单角度局限：一个问题横跨多章（"离婚时房子和孩子怎么分"跨家庭关系与离婚两节）；③ 原理型迷失：问"为什么"却召回具体条文（"为什么诉讼时效是三年"直查可能落到某条而非一般规定）；④ 检索失败硬答：query 超出三编范围（"民法典第一千条是什么"属人格权编，本库未收录）时，top-3 仍会返回"最不相关里最相关的"，模型据此硬答就是幻觉。四种故障分别由下面四个机制对症。

### 2. 机制清单：故障 → 法律例子 → 离线确定性实现 → 预期成本

| 机制 | 解决的故障 | 法律场景例子 | 离线 Mock 如何确定性实现 | 真实模式预期成本 |
|---|---|---|---|---|
| 策略路由 `route()` | 用一把锤子抡所有钉子 | "民法典第680条"直查即可，无需改写 | 纯正则（含条号/法名→direct；口语无术语→hyde；"和/还有/分别"多子问→fusion；"为什么/原理"→stepback），0 LLM | 0 |
| HyDE 假设文档 | 词汇失配 | 口语问"朋友借钱拖着不还"先生成一段假设答案文本再检索，与条文语体对齐 | `MockLLMClient` 按 purpose 分发（见 `app/llm/client.py` 的 `chat`），为 purpose="hyde" 加查表脚本：query 关键词→固定假设文档，HashEmbedder 编码结果确定 | +1 次生成 |
| RAG-Fusion 多查询 | 单角度局限 | "离婚时房子和孩子怎么分" 扩成 财产分割/抚养权/房产归属 3 个子查询，RRF（k=60）融合排名 | purpose="fusion_expand" 返回固定 3 个改写；RRF 是纯代码排序，天然确定 | +1 次生成 +3×检索 |
| Step-Back 抽象提问 | 原理型迷失 | "为什么诉讼时效是三年" 先退到"诉讼时效的一般规定"再检索 | purpose="stepback" 查表映射到章级关键词（时效→一般规定） | +1 次生成 |
| 粗召回 top-20 + Rerank 精排 top-3 | 双塔向量精度上限：同章邻块混入 top-3 | "民间借贷利率上限"粗召回混入保证合同/借款合同多块，精排应把 `借款合同（第680条）` 顶到第一 | 规则打分器纯代码：条号精确命中 > section 标题词重叠 > 向量分，完全确定 | cross-encoder 20 对一次，或 LLM listwise 1 次 |
| Self-RAG 三问 | 检索失败硬答 | "第一千条"未入库：第②问判不相关 → 换策略 → 仍不相关 → 如实说"未收录"（呼应 `query_law_article` 的"未收录"显式失败设计） | 三问全部规则化：①法条/知识类正则 ②query 与 chunk 词重叠率阈值 ③答案来源核对（复用 `app/agent/safety/citation.py::validate_citations` 的来源比对思路） | +1 次判定（或 token 级反思） |

### 3. 四个改写机制的工作示例（法律语料走一遍）

```
query: "朋友借钱拖着一直不还，电话也不接"（口语，与条文零字面重叠）

HyDE:      先让 LLM 写一段"假设答案"：
           "借款合同到期后借款人应当按照约定返还借款；逾期不还的，
            出借人可以请求支付逾期利息……"（语体已与条文对齐）
           ──encode──► 检索命中 民法典-合同#借款合同（第667-679条）
           （groundtruth 第 2 条"借款什么时候要返还"正是锚定此小节）

Fusion:    改写成 3 个子查询〔借款返还期限 / 逾期利息 / 借条证据效力〕
           各自召回 top-20 → RRF 融合：
           score(d) = Σ_q 1/(60 + rank_q(d))     （60 是平滑常数，
           让第 1 名与第 2 名差距不至于压垮其他查询的投票）
           → 跨章证据类问题也能把相关块聚合上来

Step-Back: query"为什么诉讼时效是三年" → 后退问"诉讼时效制度的一般规定"
           → 命中各编"一般规定"章节，而不是随机一条具体条文

Self-RAG:  query"民法典第一千条是什么"（人格权编，本库未收录）
           ①要检索吗：是（法条类） ②片段相关吗：top-3 与 query 词重叠率
           低于阈值 → 判不相关 ③答案被支持吗：无从谈起
           → 换策略重试一次 → 仍不达标 → 显式回复"未收录于本知识库"
```

注意：四者不是并列堆叠，而是由路由按 query 形态择一进入改写段；Rerank 与 Self-RAG 对所有策略共用。

### 4. 不达标换策略重试：预算是硬约束

自适应最怕失控放大成本，所以预算写死：每个 query 最多 2 个策略、检索总次数上限 6、LLM 调用上限 4（含改写/精排/判定）。fallback 链固定：direct 不达标 → hyde 重试一次；fusion 不达标 → 退回 direct+rerank；两轮都不达标 → 返回低置信结果并让 `search_knowledge` 的调用方在回复里明示"未在知识库找到充分依据"——检索层承认失败，比幻觉安全。这个"失败也显式"的原则与 `test_law_article_miss_is_explicit`（未命中≠异常）一脉相承。

### 5. 机制对比评估表：让 groundtruth 的 12 条标注干活

`app/evaluation/corpus_groundtruth.json` 的 `_说明` 已写明用途：query → 应命中的 文档#小节，供 Phase 2 检索质量评估（hit-rate/MRR）使用。评估脚本设计（未实现，暂名 `app/scripts/run_retrieval_eval.py`）：

- 指标：`hit-rate@3`（top-3 是否含 expected）与 `MRR`（首个命中的倒数排名）；
- 命中判定：`f"{h.chunk.doc}#{h.chunk.section}" in expected`——`Chunk.doc/section` 与标注格式同构，`TestRag::test_groundtruth_file_valid` 已保证文档名真实存在；
- 同题对比：12 条 query 逐一跑 基线直查 / +Rerank / +HyDE / +Fusion / +Step-Back / 自适应组合，输出成表：

| 机制（示意，数字待跑） | hit-rate@3 | MRR | 平均额外 LLM 调用 | 平均检索次数 |
|---|---|---|---|---|
| 直查 top-3（基线） | 待测 | 待测 | 0 | 1 |
| 直查 + Rerank | 待测 | 待测 | 0 或 1 | 1 |
| HyDE / Fusion / Step-Back | 待测 | 待测 | 1 | 1~3 |
| 自适应组合（路由+精排+反思） | 待测 | 待测 | ≤4 | ≤6 |

- 护栏断言：任何新机制在 12 条上 hit-rate/MRR 不得低于基线，否则该策略从路由表摘除——机制栈默认只开 direct+rerank，其余按数据逐个放行；
- 两栏制：离线（HashEmbedder，验证管线确定性与成本计数）与真实 embedding（验证语义收益）分开汇报，避免把散列巧合当语义能力。

### 6. 哪些缓做，为什么

- **RAPTOR（层级摘要树检索）**：需要百块以上语料且摘要质量决定上限；当前 94 块、ingest 已做同章聚合（层级信息部分内建），树化收益有限。等 Roadmap 的"指导案例全量入库"扩充语料后再评估。
- **ColBERT（后期交互排序）**：需要专用模型与逐向量存储，`VectorBackend` 契约要为 maxsim 检索改造（`app/agent/rag/backends/`），且 HashEmbedder 无法离线模拟其收益——依赖重、收益不可本地验证，缓。
- **前置依赖先行**：Roadmap 第三项"真实 embedding 接入"是所有语义机制的前提；顺序上应先接真实向量、拿到基线数字，再谈机制取舍。

### 7. 落地顺序与接口约束

落地顺序（评估先行，没有基线数字就无法证明任何机制值得开）：① 评估脚本跑出基线 hit-rate/MRR → ② Rerank（纯代码、收益最确定）→ ③ 路由 + Self-RAG 规则版 → ④ HyDE/Fusion/Step-Back（各配 Mock 脚本与单测）→ ⑤ 真实 embedding 后重跑对比表定稿路由规则。接口约束：`AdaptiveRetriever` 采用组合而非改写——不修改 `KnowledgeRetriever.search` 现有契约与 `search_knowledge` 工具签名，评估用例（如 `test_retrieval_tort_hits_target`）与 18 条评估链路零改动即可回归。规划接口草案（已实现于 app/agent/rag/adaptive.py）：

```python
class AdaptiveRetriever:                       # 规划，代码未实现
    def __init__(self, base: KnowledgeRetriever, client: BaseLLMClient): ...
    def route(self, query: str) -> str: ...    # direct / hyde / fusion / stepback
    def search(self, query: str, top_k: int = 3) -> tuple[list[RetrievedChunk], dict]:
        """meta 记录 {"strategy", "attempt", "self_rag"}，供 tracer 与评估消费。"""
```

## 知识点与面试考点

1. HyDE 的原理与代价：为什么"先编一个假设答案再检索"能缓解词汇失配，何时反而引入偏差。
2. RAG-Fusion 与 RRF：多查询召回排名如何用倒数排名融合（k=60 的作用），比加权平均好在哪。
3. Step-Back Prompting：抽象提问适用"原理/制度类"问题，法条定位类问题用它会失焦——路由的意义就在此。
4. Rerank 的两段式架构：双塔召回（快而粗）+ cross-encoder 精排（慢而准），为什么不能全程 cross-encoder。
5. Self-RAG 的反思三问（需要检索吗/片段相关吗/答案被支持吗）与 CRAG 的"纠正性检索"区别。
6. 自适应检索的预算控制：策略数/检索次数/LLM 调用上限如何防止自适应变"成本放大器"。
7. 检索评估指标：hit-rate@k 与 MRR 的计算，以及为什么需要 groundtruth 而不是肉眼判断检索质量。
8. 离线确定性测试设计：Mock 按 purpose 分发 + 查表脚本，让含 LLM 环节的管线也能全离线断言。

## 动手练习

1. 写出基线评估脚本雏形：加载 `corpus_groundtruth.json`，用 `build_retriever` 对 12 条 query 跑 `search(query, top_k=3)`，按 `f"{chunk.doc}#{chunk.section}"` 判命中，输出 hit-rate@3 与 MRR（真实 embedding 缺失时先看离线数字的稳定性）。
2. 实现离线版 Rerank：纯规则打分（条号精确命中 > section 标题词重叠 > 原始向量分），对粗召回 top-20 重排取 top-3，写单测对比"直查 top-3 vs 粗排+精排"在 12 条上的指标差异——这是整个机制栈中唯一零 LLM 成本的机制，先拿它的数字。
3. 设计路由规则的误判用例：为 direct/hyde/fusion/stepback 各构造一条会被错误路由的 query（如"为什么第680条禁止高利放贷"既是条号又是原理型），思考优先级如何排序，并写成一个纯函数 `route(query) -> str` 的表驱动测试。
