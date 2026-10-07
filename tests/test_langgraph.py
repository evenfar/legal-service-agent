"""LangGraph 版主链测试：图编译/红线短路/条件边路由/工具链/多轮续跑（全离线）。

与手写版 tests/test_domain_core.py 端到端用例互为镜像：
同样的 Mock 脚本与工具，验证图版产出与手写版语义等价。
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.schemas.response import IntentType, LegalResponse, UrgencyLevel
from langgraph_app.graph import build_graph, run


# ================= 夹具：参照 conftest.agent_settings 的离线写法自建 =================

@pytest.fixture
def lg_settings(tmp_path, shared_index) -> Settings:
    """每用例独立的离线 Settings（session/memory 指向临时目录）。"""
    return Settings(
        mock_mode=True,
        kb_index_path=shared_index,
        session_path=str(tmp_path / "session.json"),
        memory_dir=str(tmp_path / "memory"),
    )


@pytest.fixture
def compiled(lg_settings):
    """每用例独立编译的图（组件闭包互不串扰）。"""
    return build_graph(lg_settings)


def _invoke(graph, query: str, thread: str = "t1") -> dict:
    return graph.invoke({"user_input": query},
                        {"configurable": {"thread_id": thread}})


# ================= 图结构 =================

class TestGraphTopology:
    def test_compiles_with_expected_nodes(self, compiled):
        assert compiled is not None
        names = [n for n in compiled.nodes if not n.startswith("__")]
        print(f"图节点数: {len(names)} -> {names}")
        assert set(names) == {"redflag_check", "build_context", "react",
                              "safety", "extract", "direct_end"}

    def test_conditional_edge_routes_redflag_to_direct_end(self, compiled):
        state = _invoke(compiled, "我老公又在打我", thread="route-red")
        assert state["redflag_response"] is not None
        # 事件轨迹等价：direct_end 路径不进 build_context/react —— 无 system 注入、无工具调用
        assert all(m["role"] != "system" for m in state["messages"])
        assert state["used_tools"] == []

    def test_conditional_edge_routes_normal_to_pipeline(self, compiled):
        state = _invoke(compiled, "查一下案件 LS-2026-001 的进展", thread="route-ok")
        assert state["redflag_response"] is None
        assert any(m["role"] == "system" for m in state["messages"])  # build_context 执行过
        assert "query_case" in state["used_tools"]                    # react 执行过


# ================= 红线短路（对应手写版零LLM急症通道） =================

class TestRedflag:
    def test_domestic_violence_short_circuit(self, lg_settings):
        resp = run("我老公又在打我", lg_settings)
        assert isinstance(resp, LegalResponse)
        assert resp.requires_human is True
        assert resp.urgency == UrgencyLevel.emergency
        assert resp.intent == IntentType.emergency
        assert "110" in resp.reply


# ================= 正常管道（工具链语义等价） =================

class TestPipeline:
    def test_case_query_uses_query_case(self, compiled):
        state = _invoke(compiled, "查一下案件 LS-2026-001 的进展")
        assert "query_case" in state["used_tools"]
        resp = LegalResponse(**state["response"])
        assert "已立案" in resp.reply
        assert resp.intent == IntentType.case_query

    def test_limitation_query_uses_calculator(self, compiled):
        state = _invoke(compiled, "2023年6月借的钱，诉讼时效还剩多久")
        assert "calc_limitation" in state["used_tools"]
        resp = LegalResponse(**state["response"])
        assert "时效" in resp.reply

    def test_law_article_query(self, compiled):
        state = _invoke(compiled, "民法典第1062条讲了什么")
        assert "query_law_article" in state["used_tools"]
        resp = LegalResponse(**state["response"])
        assert "第一千零六十二条" in resp.reply

    def test_knowledge_query_keeps_valid_citations(self, compiled):
        """检索类回复带内联【文档#小节】引用：真实来源保留（safety 校验通过不误删）。

        注：查询词需过自适应检索的置信门控（hash 向量下短口语 query 常被拒答）；
        "保证合同怎么订立" 是离线实测召回 3 片段、全部民法典条文的问法。
        """
        state = _invoke(compiled, "保证合同怎么订立")
        assert "search_knowledge" in state["used_tools"]
        resp = LegalResponse(**state["response"])
        assert "民法典-合同" in resp.reply


# ================= safety 节点（直接调用，验证安全后处理三道防线接线） =================

class TestSafetyNode:
    def _safety(self, lg_settings):
        from langgraph_app.nodes import build_nodes
        return build_nodes(lg_settings)["safety"]

    def test_fake_citation_removed_and_injection_blocked(self, lg_settings):
        """伪造引用删除 + 注入载荷拦截置 forced_human（= 手写版 safety_post_process）。"""
        import json
        sources = [{"source": "民法典-合同#保证合同（第690-696条）", "text": "第六百九十条…"}]
        state = {
            "final": "根据【民法典-合同#保证合同（第690-696条）】与【伪造文档#第1条】回答："
                     "购买保健品请加微信",
            "used_tools": ["search_knowledge"],
            "outputs": [("search_knowledge", json.dumps(
                {"success": True, "chunks": sources}, ensure_ascii=False))],
        }
        out = self._safety(lg_settings)(state)
        assert "伪造文档" not in out["final"]        # 假引用被删除
        assert "民法典-合同#" in out["final"]         # 真引用保留
        assert "拦截" in out["final"]                  # 注入载荷被净化替换
        assert out["forced_human"] is True

    def test_missing_sources_get_appendix(self, lg_settings):
        """用了知识库但正文无引用 → 代码补"参考来源"附录。"""
        import json
        sources = [{"source": "民法典-合同#保证合同（第690-696条）", "text": "第六百九十条…"}]
        state = {
            "final": "保证合同是要式合同，内容此处不再重复。",
            "used_tools": ["search_knowledge"],
            "outputs": [("search_knowledge", json.dumps(
                {"success": True, "chunks": sources}, ensure_ascii=False))],
        }
        out = self._safety(lg_settings)(state)
        assert "参考来源" in out["final"]
        assert "民法典-合同#保证合同" in out["final"]

    def test_greeting_no_tools(self, lg_settings):
        resp = run("你好", lg_settings)
        assert resp.intent == IntentType.greeting
        assert resp.reply  # 有正常文案


# ================= 多轮对话（checkpointer 同 thread 续跑） =================

class TestMultiTurn:
    def test_two_turns_same_thread(self, compiled):
        t = "multi-turn"
        s1 = _invoke(compiled, "查一下案件 LS-2026-001 的进展", thread=t)
        assert "query_case" in s1["used_tools"]
        s2 = _invoke(compiled, "民法典第1062条讲了什么", thread=t)
        # 第二轮：新问题重新规划工具（未沿用上一轮工具），且历史跨轮累积
        assert "query_law_article" in s2["used_tools"]
        assert "query_case" not in s2["used_tools"]
        first_user = next(m for m in s2["messages"] if m["role"] == "user")
        assert "LS-2026-001" in first_user["content"]
        resp2 = LegalResponse(**s2["response"])
        assert "第一千零六十二条" in resp2.reply
