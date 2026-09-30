# legal-service-agent：法律咨询助手「小律」

> 以法律咨询为场景的 Agent 项目，两个差异化卖点：**真实公有领域语料（民法典三编700条+最高法指导案例37件）的 RAG 管线** 与 **法律场景的引用溯源/时效计算**。
> 机制层完整复用姊妹项目 [med-service-agent](../med-service-agent)（LLM重试/ReAct/记忆/技能/评估/安全五层），数据层全部法律域重写。全部离线可跑，零 API 成本。

## 快速开始（conda 环境复用 med-agent）

```bash
conda activate med-agent
cd legal-service-agent

python main.py --mock            # 单 Agent 模式
python main.py --mock --multi    # Multi-Agent（婚姻家事/合同债务/侵权赔偿/紧急通道）
python main.py --mock --trace    # 逐层打印 LLM 请求/工具调用/结构化提取

pytest                           # 64 个离线测试
python -m app.scripts.run_eval   # 18 条黄金用例评估（含4条安全红线）
```

试试：`查案件 LS-2026-001`、`民法典第1062条讲了什么`、`2023年6月借的钱时效还剩多久`、`前男友威胁要曝光我的照片`（红线短路演示）。

## 法律域特色

| 组件 | 说明 |
|---|---|
| **语料管线** `scripts/ingest_laws.py` | 民法典官方文本(700条)→ 编/章/条解析 → **同章条文主题聚合切块**（每块带条号区间标签）→ 校验(条文守恒/免责声明/结构) → 建索引 |
| **法条双通道** | query_law_article 结构化直查（条号归一化"第1062条"→"第一千零六十二条"）+ search_knowledge 语义检索，关键词保精确、语义保泛化 |
| **诉讼时效计算器** | 确定性日期运算（一般3年/劳动仲裁1年等），临近90天警示——"确定性工作不交给模型"的典型应用 |
| **法律红线** | 人身安全(报警/保护令) > 证据灭失(立即固定) > 时效临界(强制精算)，规则短路零LLM |
| **类案检索** | 最高法指导性案例裁判要点 + "个案有差异"边界提示 |
| **记忆档案** | 法律分类（在办案件/关键日期/财产状况）+ 防投毒校验 |

## 架构（复用与重写边界）

```
复用自 med-service-agent（自有代码）：llm/(重试+Mock) agent/base(ReAct唯一实现)
  rag/(双后端embedder) memory/ skills/loader evaluation/(沙箱+2×2指标) safety框架
法律域重写：schemas/LegalResponse · 9个工具 · 4子Agent · 红线规则
  3个SKILL.md(借贷维权/离婚/劳动仲裁) · Mock脚本 · 评估18例+groundtruth
```

## 语料与合规

- 民法典三编（合同526/婚姻家庭79/侵权责任95条），2021施行版官方公开文本，公有领域；
- 知识库含免责声明块，全库定位"科普级程序指引，不构成法律意见"；
- `raw/SOURCES.md` 记录获取来源；最高法指导案例37件已入库（CC0开源库+官网逐案溯源）。

## 评估成绩（离线规则模式）

单 Agent 与 Multi-Agent 双模式均 **18/18 通过（过程分/结果分 1.00）**，含 4 条安全红线用例一票否决。
`app/evaluation/corpus_groundtruth.json` 已标注 12 条检索地面真值，供 Phase 2 检索质量评估(hit-rate/MRR)。

## Roadmap

- **Phase 2（已实现离线版）**：AdaptiveRetriever 机制栈上线——策略路由 + HyDE/Fusion/Step-Back + Rerank + Self-RAG门 + 预算护栏（`app/agent/rag/adaptive.py`）。
  实测（12条groundtruth，离线Hash向量）：hit@3 基线0.33 → +Rerank 0.58 → 自适应 **0.75**；MRR 0.18→0.51；额外LLM成本 0。
  对比命令：`python -m app.scripts.run_retrieval_eval --strict`。待续：真实 embedding 列重跑对比表
- 工程化：CI / Docker / FastAPI 服务化（与 med 项目共用升级路径）
