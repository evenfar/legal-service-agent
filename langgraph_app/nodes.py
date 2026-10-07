"""图节点函数：把 app/agent/base.py + chat.py 的方法链改写为纯函数。

复用而非复制——节点只做"编排"，领域能力全部来自 app/ 现有组件：
build_client（LLM/Mock）、build_tool_registry（法律九件套）、app.safety（红线/注入/引用）、
SkillManager（技能目录）、LegalResponse（结构化输出）。与手写版的逐条映射见 README.md。
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from app.agent.rag.retriever import build_retriever
from app.agent.safety import (append_sources_if_missing, detect_red_flags,
                              emergency_reply, scan_output, validate_citations)
from app.agent.skills.loader import SkillManager
from app.agent.tools.registry import ToolDeps, build_tool_registry
from app.agent.tracer import Tracer
from app.config.settings import Settings
from app.llm.client import build_client
from app.prompts.consultant import SYSTEM_PROMPT
from app.schemas.response import IntentType, LegalResponse, UrgencyLevel
from langgraph_app.state import GraphState

# 短路红线：人身安全 + 证据风险零 LLM 直达（= handle_redflag 的全部急症）。
# 与手写版的差异（有意为之的教学决策）：时效临界不短路——手写版给模板话术，
# 图版本让它走正常管道，由 react 节点调 calc_limitation 精确计算（工具答案优于模板）。
SHORT_CIRCUIT_FLAGS = {"家暴/暴力", "跟踪骚扰", "威胁恐吓", "非法拘禁", "抢夺",
                       "证据灭失风险", "对方否认风险"}

# 提取失败的降级文案与 base.py 的 EXTRACT_SYSTEM 同义（简化版，直接内联）
_EXTRACT_SYSTEM = ("基于以下法律助手回复提取结构化信息，reply 字段直接用原文。"
                   "人身安全内容（报警/110/保护令）urgency=emergency 且 requires_human=true。")


def build_nodes(settings: Settings) -> dict[str, Callable[[GraphState], dict]]:
    """装配节点闭包：LLM/工具注册表/技能目录按 settings 建一次，五个节点共享。"""
    tracer = Tracer()
    client = build_client(settings, tracer)
    skill_manager = SkillManager(settings.skills_dir, settings.skills_enabled)
    retriever = build_retriever(settings, tracer, client=client)
    registry = build_tool_registry(ToolDeps(
        tracer=tracer, retriever=retriever,
        memory_manager=None, skill_manager=skill_manager))

    # ---------- 节点1：红线检查（= BaseAgentRuntime.handle_redflag） ----------
    def redflag_check(state: GraphState) -> dict:
        """每轮入口：重置轮内字段 + 规则红线检测（零 LLM、零延迟、宁可误报）。"""
        user_input = state.get("user_input", "")
        history = [m for m in state.get("messages", [])
                   if m.get("role") != "system"]   # 丢弃上一轮的 system 前缀
        history.append({"role": "user", "content": user_input})
        redflag_response: Optional[LegalResponse] = None
        hits = [f for f in detect_red_flags(user_input) if f in SHORT_CIRCUIT_FLAGS]
        if hits:
            tracer.log("redflag_shortcut", flags=hits)
            redflag_response = LegalResponse(
                intent=IntentType.emergency, confidence=1.0,
                reply=emergency_reply(hits), requires_human=True,
                urgency=UrgencyLevel.emergency)
        return {"messages": history, "redflag_response": redflag_response,
                "final": "", "used_tools": [], "outputs": [],
                "forced_human": False, "response": None}

    # ---------- 节点2：上下文组装（= chat() 的 system 拼接段） ----------
    def build_context(state: GraphState) -> dict:
        """system prompt + 技能目录注入，产出 LLM-ready 的消息列表。"""
        system = SYSTEM_PROMPT
        if skill_manager.enabled:
            system += skill_manager.build_catalog_prompt()
        return {"messages": [{"role": "system", "content": system}]
                + state["messages"]}

    # ---------- 节点3：ReAct 循环（= BaseAgentRuntime.react，独立简化实现） ----------
    def react(state: GraphState) -> dict:
        """≤max_react_steps：chat→有 tool_calls 则执行回填→无则 final。"""
        messages = list(state["messages"])
        used: list[str] = []
        outputs: list[tuple[str, str]] = []
        tools = registry.definitions
        for _ in range(settings.max_react_steps):
            resp = client.chat(messages, tools=tools, purpose="react")
            if not resp.tool_calls:
                return {"messages": messages, "final": resp.content,
                        "used_tools": used, "outputs": outputs}
            messages.append({"role": "assistant", "content": resp.content,
                             "tool_calls": [{"id": tc["id"], "type": "function",
                                             "function": {"name": tc["name"],
                                                          "arguments": json.dumps(
                                                              tc["arguments"],
                                                              ensure_ascii=False)}}
                                            for tc in resp.tool_calls]})
            for tc in resp.tool_calls:
                out = registry.execute(tc["name"], tc["arguments"])
                used.append(tc["name"])
                outputs.append((tc["name"], out))
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": out})
        # 步数用尽：去掉工具再给一次收尾机会（与手写版 react() 的防截断语义一致）
        resp = client.chat(messages, purpose="react")
        messages.append({"role": "assistant", "content": resp.content})
        return {"messages": messages, "final": resp.content,
                "used_tools": used, "outputs": outputs}

    # ---------- 节点4：安全后处理（= safety_post_process 的简化版） ----------
    def safety(state: GraphState) -> dict:
        """出站三道处理：注入扫描 → 引用校验 → 缺失来源补全（NLI 核验见 README）。"""
        final = state.get("final", "")
        used = state.get("used_tools", [])
        outputs = state.get("outputs", [])
        forced_human = False
        if "search_knowledge" in used:
            safe, cleaned = scan_output(final)
            if not safe:
                final, forced_human = cleaned, True
                tracer.log("injection_blocked", original_len=len(final))
            sources: list[str] = []
            for name, out in outputs:
                if name == "search_knowledge":
                    try:
                        sources += [c["source"] for c in json.loads(out).get("chunks", [])
                                    if isinstance(c, dict)]
                    except (json.JSONDecodeError, AttributeError):
                        pass
            final, fakes = validate_citations(final, sources)
            if fakes:
                tracer.log("fake_citations_removed", fakes=fakes)
            final = append_sources_if_missing(final, sources)
        return {"final": final, "forced_human": forced_human}

    # ---------- 节点5：结构化提取（= extract_structured 简化版） ----------
    def extract(state: GraphState) -> dict:
        """final 文本 → LegalResponse；提取失败降级原文+转人工（优雅收尾）。"""
        text = state["final"]
        try:
            result = client.parse_structured(
                [{"role": "system", "content": _EXTRACT_SYSTEM},
                 {"role": "user", "content": text}], LegalResponse, purpose="extract")
        except Exception:  # noqa: BLE001 —— 一次提取失败不丢弃已完成的回复
            tracer.log("extract_fallback_raw", chars=len(text))
            result = LegalResponse(reply=text, requires_human=True, confidence=0.3)
        if result.reply != text:      # 正文以安全后处理后的原文为准（审查#16）
            result.reply = text
        if state.get("forced_human"):
            result.requires_human = True
        return {"response": result.model_dump(mode="json"),
                "messages": state["messages"]
                + [{"role": "assistant", "content": result.model_dump_json(
                    ensure_ascii=False)}]}

    # ---------- 终点：红线直达（= chat() 里 emergency 分支的 finish_turn） ----------
    def direct_end(state: GraphState) -> dict:
        """红线命中：跳过 react/safety/extract，急诊应答直接落盘进历史。"""
        resp: LegalResponse = state["redflag_response"]
        return {"final": resp.reply, "response": resp.model_dump(mode="json"),
                "messages": state["messages"]
                + [{"role": "assistant", "content": resp.model_dump_json(
                    ensure_ascii=False)}]}

    return {"redflag_check": redflag_check, "build_context": build_context,
            "react": react, "safety": safety, "extract": extract,
            "direct_end": direct_end}
