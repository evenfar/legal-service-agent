# LangGraph 版主链：与手写版的概念映射（教学资产）

> 同一条法律咨询主链的第二种写法：手写版（`app/agent/`）用类与 if/for 编排，
> 本目录用 LangGraph `StateGraph` 声明式编排。**零复制**——LLM 客户端、工具注册表、
> 安全模块、技能目录、LegalResponse 全部 import 自 `app/`，改一处两版同时受益。
> 读法：左列是我手写的机制，右列是框架把同一件事做掉的声明式方式。

## 图结构

```
                          ┌──────────────────────────────────────────┐
                          │            (未命中红线)                   │
START ──> redflag_check ──┤                                          │
            (规则检测,     │      build_context ─> react ─> safety ─> extract ─> END
             零LLM)       │      (system+技能目录)  (ReAct)  (注入/引用)  (LegalResponse)
                          │
                          └──(命中红线)──> direct_end ──────────────────────> END
                                          (急诊应答直达, 跳过 LLM)
```

## 概念映射：手写版 ↔ LangGraph

| 手写版（文件/函数） | LangGraph 对应 | 说明 |
|---|---|---|
| `BaseAgentRuntime.__init__` 装配 client/tools | `build_graph()`：`add_node` + `compile` | 我在构造器里搭骨架，LangGraph 编译一张图 |
| `chat.py chat()` 的隐式流水线 | `StateGraph` 显式节点+边 | 控制流从"读代码追调用"变成"看图即得" |
| `raw_messages` 等实例属性 | `state.py GraphState`(TypedDict) | 中间产物收敛为显式 state，节点返回局部更新由框架合并 |
| `handle_redflag()` 返回 None/急症应答 | `redflag_check` 节点 + **条件边** `route_after_redflag` | if emergency is not None 的分流，图里是一条声明式路由边 |
| `react()` 的 for 循环 | `react` **单节点内的循环** | 本项目刻意把 ReAct 收在单节点（工具轨迹是模型私事）；展开成 LLM节点→tool节点→条件回边 是官方 prebuilt `create_react_agent` 的画法，留作练习 |
| `max_react_steps` 步数护栏 | 同款参数（循环在节点内） | 展开成图节点时对应 `RecursionLimit` |
| `safety_post_process()` | `safety` 节点（管道一环） | 注入扫描/引用校验/来源补全，出站处理是图上可见的一步 |
| `extract_structured()` + 降级 | `extract` 节点 | parse_structured 失败→原文+转人工的优雅收尾逻辑照搬 |
| `finish_turn()` 写历史/落盘 | `extract`/`direct_end` 末尾追加 assistant 消息 + **checkpointer** | END 后的持久化由框架接管（见下） |
| `storage.py save/load_session` | `MemorySaver` checkpointer（thread_id） | 它存每个 super-step 后的 state，粒度比整段会话细；生产换 SqliteSaver/PostgresSaver |
| `Tracer` JSONL 计量 | callbacks / LangSmith（**未接**） | 图自动产出每步事件流，接 LangSmith 即有回放；本项目保留 Tracer 以复用评估沙箱 |
| CLI 的 `input()` 审批（med 版） | `interrupt()` + `Command(resume=...)` | 同一个"暂停-持久化-等输入-恢复"模式，框架原生支持 HITL |
| Mock 测试的事件断言 | `graph.get_state(config)` / stream 事件 | 红线短路"没进 react"可以直接从 state 断言（见 tests） |

## 与手写版的两处刻意差异

1. **时效临界不短路**：手写版任何红线（含"快三年了"）都走模板急症话术；
   图版 `SHORT_CIRCUIT_FLAGS` 只含人身安全+证据风险，时效临界放行进 react——
   由 `calc_limitation` 精确计算比模板更有用。这是"条件边路由是数据驱动的"
   的演示：改一个集合，图的流向就变。
2. **记忆/压缩未搬**：`MemoryManager` 与历史压缩属会话层能力，checkpointer
   已覆盖持久化语义，搬进来只会让教学图变胖。多轮续跑用同 thread_id 即可。

## 运行

```bash
python -c "from app.config.settings import Settings; from langgraph_app import run; \
           print(run('查一下案件 LS-2026-001 的进展', Settings(mock_mode=True)))"
pytest tests/test_langgraph.py -q     # 12 条离线用例
```

`build_graph(settings)` 工厂 + `run(query, settings)` 便捷入口；多轮对话传固定
`thread_id` 给 `run(..., thread_id=...)` 或直接 `graph.invoke({"user_input": q}, config)`。

## 版本说明

实测 **langgraph 1.2.14 / langchain-core 1.6.7 / pydantic 2.13.5**（conda `med-agent`）：
`StateGraph`/`START`/`END`、`add_conditional_edges`、`langgraph.checkpoint.memory.MemorySaver`
均按 1.x 稳定 API 可用，无需 0.x 时代的写法差异处理；`graph.get_graph().draw_ascii()`
需要额外安装 grandalf，本 README 的结构图是手绘的。

## 什么时候选框架（结论）

- **这条主链本身**：节点 ≤6、无并行扇出、无断点续跑需求 → 手写版 200 行更直白，
  调试不打穿抽象层——本项目 main 分支选手写是对的；
- 需要 **HITL 审批/断点续跑/时间旅行调试**，或团队要 **LangSmith 可观测生态** → LangGraph 的
  checkpointer + interrupt 是自研成本最高的那 30%，值得用框架；
- 图会长大（并行研究波、动态扇出、Critic 返工环）→ 框架的 Send API/reducer/可视化
  开始显著便宜过手写线程池与锁；
- 两个版本的测试互为镜像（同 Mock 脚本、同样断言"红线零工具调用"）——
  **机制理解看手写版，工程选型看图版**，这就是双版本作为教学资产的价值。
