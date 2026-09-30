"""法律域核心链路测试：LLM Mock / 工具 / 安全 / 端到端（全离线）。"""

from __future__ import annotations

import json

import pytest

from app.agent.tracer import Tracer
from app.config.settings import Settings
from app.llm.client import MockLLMClient
from app.schemas.response import IntentType, LegalResponse, UrgencyLevel


# ================= LLM 层（重试复用 med 测试，这里测法律脚本） =================

class TestMockScripts:
    def _mk(self):
        return MockLLMClient(tracer=Tracer())

    def test_route_four_classes(self):
        c = self._mk()
        assert c.chat([{"role": "system", "content": "s"},
                       {"role": "user", "content": "他威胁要曝光我照片"}],
                      purpose="router").content == "emergency"
        assert c.chat([{"role": "system", "content": "s"},
                       {"role": "user", "content": "离婚孩子归谁"}],
                      purpose="router").content == "family"
        assert c.chat([{"role": "system", "content": "s"},
                       {"role": "user", "content": "车祸受伤怎么赔"}],
                      purpose="router").content == "tort"
        assert c.chat([{"role": "system", "content": "s"},
                       {"role": "user", "content": "朋友欠钱不还"}],
                      purpose="router").content == "contract"

    def test_plan_case_query(self):
        plan = MockLLMClient._plan("查一下案件 LS-2026-001 的进展")
        assert plan == [("query_case", {"case_id": "LS-2026-001"})]

    def test_plan_limitation_extracts_date(self):
        plan = MockLLMClient._plan("2023年6月借的钱，诉讼时效还剩多久")
        assert plan[0][0] == "calc_limitation"
        assert plan[0][1]["start_date"] == "2023-06-01"
        assert plan[0][1]["case_type"] == "一般民事"

    def test_plan_labor_limitation(self):
        plan = MockLLMClient._plan("2025年12月被拖欠工资，时效还剩多久")
        assert plan[0][1]["case_type"] == "劳动仲裁"

    def test_plan_article_number_normalized(self):
        plan = MockLLMClient._plan("民法典第1062条讲了什么")
        assert plan == [("query_law_article", {"query": "第1062条"})]

    def test_plan_precedents_topic_extraction(self):
        plan = MockLLMClient._plan("有没有类似车祸体质差的判例")
        assert plan == [("search_precedents", {"query": "交通事故"})]

    def test_legal_fields_intent_mapping(self):
        f = MockLLMClient._legal_fields("《民法典·合同编》第六百八十条：禁止高利放贷")
        assert f["intent"] == IntentType.law_article
        f2 = MockLLMClient._legal_fields("请立即拨打110报警并申请保护令")
        assert f2["intent"] == IntentType.emergency
        assert f2["requires_human"] is True
        assert f2["urgency"] == UrgencyLevel.emergency

    def test_repeat_question_resets_counter(self):
        c = self._mk()
        sys_msg = {"role": "system", "content": "s"}
        tools = [{"type": "function", "function": {"name": "query_case"}}]
        msgs = [sys_msg, {"role": "user", "content": "查案件 LS-2026-001"}]
        r1 = c.chat(msgs, tools=tools, purpose="react")
        assert r1.tool_calls[0]["name"] == "query_case"
        # 第二步出 final；同轮再问一次，工具调用应重新出现（回归测试）
        tool_result = json.dumps({"success": True, "case": {
            "case_id": "LS-2026-001", "type": "民间借贷纠纷",
            "court": "某法院", "stage": "已立案", "next_action": "开庭"}})
        hist = msgs + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "query_case",
                                          "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": tool_result},
            {"role": "user", "content": "查案件 LS-2026-001"}]
        r2 = c.chat(hist, tools=tools, purpose="react")
        assert r2.tool_calls[0]["name"] == "query_case"


# ================= 工具层 =================

@pytest.fixture(autouse=True)
def fresh_data():
    from app.agent.tools.mock_data import reset_mock_data
    reset_mock_data()
    yield
    reset_mock_data()


class TestTools:
    def test_build_registry_nine_tools(self):
        from app.agent.tools.registry import ToolDeps, build_tool_registry
        reg = build_tool_registry(ToolDeps())
        assert set(reg.names()) == {
            "query_case", "update_case_note", "query_law_article",
            "search_precedents", "calc_limitation", "query_court",
            "search_knowledge", "recall_user_memory", "load_skill"}

    def test_query_case(self):
        from app.agent.tools.case import query_case
        out = query_case("LS-2026-001")
        assert out["success"] and out["case"]["type"] == "民间借贷纠纷"
        assert not query_case("LS-2026-999")["success"]

    def test_update_case_note_mutates_state(self):
        from app.agent.tools.case import update_case_note
        from app.agent.tools.mock_data import CASES
        out = update_case_note("LS-2026-001", "对方提交虚假考勤")
        assert out["success"] and CASES["LS-2026-001"]["notes"]

    def test_law_article_number_normalization(self):
        from app.agent.tools.law import query_law_article
        out = query_law_article("第1062条")
        assert out["success"] and out["article"]["topic"] == "夫妻共同财产"
        assert out["match_type"] == "条号归一化匹配"

    def test_law_article_topic(self):
        from app.agent.tools.law import query_law_article
        out = query_law_article("抚养权")
        assert out["success"] and out["article"]["article"] == "第一千零八十四条"

    def test_law_article_miss_is_explicit(self):
        from app.agent.tools.law import query_law_article
        out = query_law_article("量子力学")
        assert not out["success"] and "未收录" in out["error"]

    def test_calc_limitation_near(self):
        from app.agent.tools.limitation import calc_limitation
        out = calc_limitation("一般民事", "2023-10-15")  # 距今约1年内? 手工验证
        assert out["success"]
        assert isinstance(out["days_left"], int)
        assert out["warning"] == (out["days_left"] <= 90)

    def test_calc_limitation_invalid_date(self):
        from app.agent.tools.limitation import calc_limitation
        assert not calc_limitation("一般民事", "2023/10/15")["success"]
        assert not calc_limitation("不存在的类型", "2023-10-15")["success"]

    def test_precedents_match_and_miss(self):
        from app.agent.tools.precedents import search_precedents
        hit = search_precedents("交通事故")
        assert hit["success"] and hit["precedents"]
        miss = search_precedents("完全无关主题xyz")
        assert miss["success"] and miss["precedents"] == []  # 未命中≠异常

    def test_court_lookup(self):
        from app.agent.tools.court import query_court
        assert query_court("仲裁")["success"]
        assert not query_court("火星法院")["success"]


# ================= 安全 =================

class TestRedFlags:
    def test_threat(self):
        from app.agent.safety.redflag import detect_red_flags, emergency_reply
        flags = detect_red_flags("前男友威胁要曝光我的照片")
        assert "威胁恐吓" in flags
        assert "110" in emergency_reply(flags)

    def test_domestic_violence(self):
        from app.agent.safety.redflag import detect_red_flags, emergency_reply
        flags = detect_red_flags("我老公又在打我")
        assert "家暴/暴力" in flags
        reply = emergency_reply(flags)
        assert "110" in reply and "保护令" in reply

    def test_evidence_destruction(self):
        from app.agent.safety.redflag import detect_red_flags, emergency_reply
        flags = detect_red_flags("对方说要删除聊天记录")
        assert "证据灭失风险" in flags
        assert "固定证据" in emergency_reply(flags)

    def test_limitation_phrase(self):
        from app.agent.safety.redflag import detect_red_flags
        assert "时效临界" in detect_red_flags("2023年借的，快三年了还能起诉吗")

    def test_no_false_positive_normal(self):
        from app.agent.safety.redflag import detect_red_flags
        assert detect_red_flags("想咨询借款利率问题") == []


# ================= 端到端（离线 Mock 全链路） =================

@pytest.fixture
def agent(agent_settings):
    from app.agent.chat import LegalAgent
    a = LegalAgent(agent_settings)
    yield a
    a.close()


class TestEndToEnd:
    def _tools(self, agent):
        return [e["tool"] for e in agent.tracer.tool_calls]

    def test_greeting(self, agent):
        r = agent.chat("你好")
        assert r.intent == IntentType.greeting
        assert self._tools(agent) == []

    def test_case_query_pipeline(self, agent):
        r = agent.chat("查一下案件 LS-2026-001 的进展")
        assert "民间借贷" in r.reply and "已立案" in r.reply
        assert self._tools(agent) == ["query_case"]
        assert r.intent == IntentType.case_query

    def test_redflag_shortcut_zero_llm(self, agent):
        r = agent.chat("我老公又在打我，怎么办")
        assert r.urgency == UrgencyLevel.emergency
        assert r.requires_human is True
        assert "110" in r.reply
        assert agent.tracer.llm_calls == []  # 规则短路：零LLM调用
        assert agent.tracer.tool_calls == []

    def test_limitation_tool_flow(self, agent):
        r = agent.chat("2023年6月借的钱，诉讼时效还剩多久")
        assert "calc_limitation" in self._tools(agent)
        assert "时效截止" in r.reply

    def test_article_flow(self, agent):
        r = agent.chat("民法典第1062条讲了什么")
        assert "query_law_article" in self._tools(agent)
        assert "夫妻" in r.reply and "第六十二条" not in r.reply

    def test_knowledge_with_sources(self, agent):
        r = agent.chat("交通事故责任的法条是怎么规定的")
        assert "search_knowledge" in self._tools(agent)
        assert "参考来源" in r.reply
        assert "民法典-侵权" in r.reply

    def test_write_note_chain(self, agent):
        r = agent.chat("帮案件 LS-2026-002 记录一下：对方提交了虚假考勤记录")
        assert self._tools(agent) == ["query_case", "update_case_note"]
        assert "备注" in r.reply

    def test_session_persisted(self, agent, agent_settings):
        agent.chat("查一下案件 LS-2026-001 的进展")
        from app.agent.storage import load_session
        assert load_session(agent.session_path) is not None


# ================= Multi-Agent =================

class TestMultiAgent:
    @pytest.fixture
    def orch(self, agent_settings):
        from app.multi_agent.orchestrator import MultiAgentOrchestrator
        agent_settings.multi_agent_enabled = True
        o = MultiAgentOrchestrator(agent_settings)
        yield o
        o.close()

    def test_specs_whitelists(self):
        from app.multi_agent.agents import SUBAGENT_SPECS
        assert set(SUBAGENT_SPECS) == {"family", "contract", "tort", "emergency"}
        assert "calc_limitation" not in SUBAGENT_SPECS["family"].tools
        assert len(SUBAGENT_SPECS["emergency"].tools) == 0

    def test_routes_family(self, orch):
        r = orch.chat("我要离婚，孩子才三岁抚养权怎么办")
        routes = [e["agent"] for e in orch.tracer.entries if e["type"] == "route"]
        assert routes[-1] == "婚姻家事助理"

    def test_routes_contract(self, orch):
        r = orch.chat("朋友借钱不还有借条怎么维权")
        routes = [e["agent"] for e in orch.tracer.entries if e["type"] == "route"]
        assert routes[-1] == "合同债务助理"
        tools = {e["tool"] for e in orch.tracer.tool_calls}
        assert tools <= {"load_skill", "query_case", "query_law_article",
                         "search_precedents", "calc_limitation", "search_knowledge",
                         "recall_user_memory", "update_case_note"}

    def test_redflag_before_router(self, orch):
        r = orch.chat("前男友威胁要曝光我的照片")
        assert r.urgency == UrgencyLevel.emergency
        assert orch.tracer.llm_calls == []
