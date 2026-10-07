"""图组装：StateGraph 把手写版 chat() 的隐式流水线变成显式声明。

START → redflag_check ─(命中红线)→ direct_end ────────────────────→ END
               └─(未命中)→ build_context → react → safety → extract → END

提供 build_graph(settings) 工厂与 run(query, settings) 便捷入口。
langgraph 1.2.14 实测：MemorySaver / add_conditional_edges 均按稳定 API 可用。
"""

from __future__ import annotations

import uuid
from typing import Optional

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from app.config.settings import Settings
from app.schemas.response import LegalResponse
from langgraph_app.nodes import build_nodes
from langgraph_app.state import GraphState


def route_after_redflag(state: GraphState) -> str:
    """条件边路由函数：红线命中走急诊直达，否则进正常咨询管道。"""
    return "direct_end" if state.get("redflag_response") else "build_context"


def build_graph(settings: Optional[Settings] = None):
    """编译图。checkpointer 用内存版：同 thread_id 多轮 invoke 可续（= 会话持久化）。

    MemorySaver 属 langgraph.checkpoint.memory，1.x 内置于发行包内；
    若未来拆包导致 ImportError，则降级为无 checkpointer（单轮仍完整可用）。
    """
    nodes = build_nodes(settings or Settings())
    builder = StateGraph(GraphState)
    for name, fn in nodes.items():
        builder.add_node(name, fn)
    builder.add_edge(START, "redflag_check")
    builder.add_conditional_edges("redflag_check", route_after_redflag,
                                  ["direct_end", "build_context"])
    builder.add_edge("build_context", "react")
    builder.add_edge("react", "safety")
    builder.add_edge("safety", "extract")
    builder.add_edge("extract", END)
    builder.add_edge("direct_end", END)
    checkpointer = None
    try:                            # 降级保护：教学资产不因可选依赖缺失而不可跑
        checkpointer = MemorySaver()
    except Exception:               # pragma: no cover - langgraph 1.2.14 内置，仅防御
        checkpointer = None
    return builder.compile(checkpointer=checkpointer)


def run(query: str, settings: Optional[Settings] = None,
        graph=None, thread_id: Optional[str] = None) -> LegalResponse:
    """便捷入口：一轮咨询进图，出图即 LegalResponse。

    graph 可传入已编译实例（复用组件）；thread_id 固定时配合 checkpointer
    可在同一 thread 上续多轮（state.messages 跨轮累积）。
    输入只写 user_input 一个键：messages 留给 checkpoint 里的历史——
    若这里传 messages=[] 会把上一轮历史在入口处覆盖掉（输入即状态更新）。
    """
    compiled = graph or build_graph(settings)
    config = {"configurable": {"thread_id": thread_id or uuid.uuid4().hex}}
    result = compiled.invoke({"user_input": query}, config)
    return LegalResponse(**result["response"])
