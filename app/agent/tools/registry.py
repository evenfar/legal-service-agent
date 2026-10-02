"""工具注册表（法律域）：OpenAI function calling 协议 + 统一执行入口。

机制与 med 版完全一致（单一事实来源 / 异常包观察 / 白名单 / tracer 计量），
仅工具集合换成法律九件套。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.agent.tools.case import query_case, update_case_note
from app.agent.tools.court import query_court
from app.agent.tools.knowledge import search_knowledge
from app.agent.tools.law import query_law_article
from app.agent.tools.limitation import calc_limitation
from app.agent.tools.memory_tool import recall_user_memory
from app.agent.tools.precedents import search_precedents
from app.agent.tools.skill_tool import load_skill
from app.agent.tracer import Tracer

_TYPE_CHECK: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., dict]

    @property
    def openai_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters}}


class ToolRegistry:
    def __init__(self, tracer: Optional[Tracer] = None):
        self._tools: dict[str, ToolDef] = {}
        self._tracer = tracer
        self.mcp_client = None  # mcp_bridge 挂载（close 时统一释放）

    def register(self, name: str, description: str, parameters: dict,
                 fn: Callable[..., dict]) -> None:
        self._tools[name] = ToolDef(name, description, parameters, fn)

    @property
    def definitions(self) -> list[dict]:
        return [t.openai_schema for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def has(self, name: str) -> bool:
        return name in self._tools

    def subset(self, allowed: set[str]) -> "ToolRegistry":
        view = ToolRegistry(self._tracer)
        view._tools = {k: v for k, v in self._tools.items() if k in allowed}
        return view

    def execute(self, name: str, arguments: dict) -> str:
        t0 = time.perf_counter()
        arguments = arguments if isinstance(arguments, dict) else {}
        tool = self._tools.get(name)
        if tool is None:
            return self._finish(name, arguments, False, t0,
                                {"error": f"未知工具: {name}，可用: {self.names()}"})
        props = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        for param in required:
            if param not in arguments:
                return self._finish(name, arguments, False, t0,
                                    {"error": f"缺少必填参数: {param}"})
        for param, value in arguments.items():
            spec = props.get(param)
            if spec and not _TYPE_CHECK.get(spec.get("type", "string"),
                                            lambda v: True)(value):
                return self._finish(name, arguments, False, t0,
                                    {"error": f"参数 {param} 类型应为 {spec.get('type')}"})
        filtered = {k: v for k, v in arguments.items() if k in props}
        try:
            result = tool.fn(**filtered)
            # ok=执行层成功（无异常）。业务级 success=False（如"未找到"）也算
            # 执行成功——错误已如实转达给模型与用户，属于工具的正确行为（审查#22语义分层）
            return self._finish(name, arguments, True, t0, result)
        except Exception as e:  # noqa: BLE001
            return self._finish(name, arguments, False, t0,
                                {"error": f"{type(e).__name__}: {e}"})

    def _finish(self, name: str, arguments: dict, ok: bool,
                t0: float, result: dict) -> str:
        if self._tracer:
            self._tracer.log_tool(name, arguments, ok,
                                  (time.perf_counter() - t0) * 1000)
        return json.dumps(result, ensure_ascii=False)


@dataclass
class ToolDeps:
    """工具层的协作对象（依赖注入，无全局单例）。"""
    tracer: Optional[Tracer] = None
    retriever: Any = None
    memory_manager: Any = None
    skill_manager: Any = None


def build_tool_registry(deps: ToolDeps) -> ToolRegistry:
    reg = ToolRegistry(deps.tracer)

    reg.register(
        "query_case",
        "查询案件进展（阶段/法院/下一步动作/关键日期）。案号格式 LS-2026-001。",
        {"type": "object",
         "properties": {"case_id": {"type": "string", "description": "案件编号"}},
         "required": ["case_id"]},
        query_case)

    reg.register(
        "update_case_note",
        "为案件补充当事人备注（如新证据说明、与对方沟通记录）。写操作，需先复述确认。",
        {"type": "object",
         "properties": {"case_id": {"type": "string"},
                        "note": {"type": "string", "description": "备注内容，一句话"}},
         "required": ["case_id", "note"]},
        update_case_note)

    reg.register(
        "query_law_article",
        "查高频法条：支持条号（第1062条）或主题词（抚养权/诉讼时效/利率）。",
        {"type": "object",
         "properties": {"query": {"type": "string", "description": "条号或主题词"}},
         "required": ["query"]},
        query_law_article)

    reg.register(
        "search_precedents",
        "检索最高人民法院指导性案例的裁判要点。类案参考必须注明个案差异。",
        {"type": "object",
         "properties": {"query": {"type": "string", "description": "案情主题词"}},
         "required": ["query"]},
        search_precedents)

    reg.register(
        "calc_limitation",
        "诉讼时效计算器（确定性日期运算）。回答时效问题必须先用本工具精确计算。"
        "case_type 从这些里选：一般民事/身体伤害索赔/劳动仲裁/交通事故索赔/行政复议。",
        {"type": "object",
         "properties": {
             "case_type": {"type": "string", "description": "案件类型"},
             "start_date": {"type": "string", "description": "起算日期 YYYY-MM-DD"}},
         "required": ["case_type", "start_date"]},
        calc_limitation)

    reg.register(
        "query_court",
        "查询法院层级管辖与立案方式（基层/中级/仲裁委/立案途径）。",
        {"type": "object",
         "properties": {"keyword": {"type": "string"}},
         "required": ["keyword"]},
        query_court)

    reg.register(
        "search_knowledge",
        "检索法律知识库（民法典三编全文/指导性案例/司法解释），返回带来源片段。"
        "法条语境、程序说明类问题必须先检索。",
        {"type": "object",
         "properties": {"query": {"type": "string"},
                        "top_k": {"type": "integer", "description": "返回条数，默认3"}},
         "required": ["query"]},
        lambda query, top_k=0: search_knowledge(deps.retriever, query, top_k))

    reg.register(
        "recall_user_memory",
        "查询用户档案（在办案件/关键日期/既往咨询）。涉及案件建议前先核对。",
        {"type": "object",
         "properties": {"query": {"type": "string"}},
         "required": []},
        lambda query="": recall_user_memory(deps.memory_manager, query))

    reg.register(
        "load_skill",
        "按名称加载维权流程技能（loan-recovery/divorce/labor-arb）。"
        "问题命中技能场景时先调用本工具。",
        {"type": "object",
         "properties": {"skill_name": {"type": "string"}},
         "required": ["skill_name"]},
        lambda skill_name: load_skill(deps.skill_manager, skill_name))

    return reg
