"""升级第二步测试：模型故障转移链 + 校准置信度三档拒答。"""

from __future__ import annotations

import json

import pytest

from app.agent.tracer import Tracer
from app.config.settings import Settings
from app.llm.client import BaseLLMClient, LLMError, LLMResponse, build_client
from app.llm.router import (BUSY_TEMPLATE, FallbackLLMClient,
                            RulesFallbackClient, build_fallback_client)


class _Stub(BaseLLMClient):
    def __init__(self, content="ok", fail=False):
        self.content = content
        self.fail = fail
        self.calls = 0

    def chat(self, messages, tools=None, temperature=None, max_tokens=None,
             purpose="react"):
        self.calls += 1
        if self.fail:
            raise LLMError("simulated outage")
        return LLMResponse(content=self.content)

    def parse_structured(self, messages, schema, purpose="extract"):
        if self.fail:
            raise LLMError("simulated outage")
        return schema(reply=self.content)


class TestFallbackChain:
    def test_primary_ok_no_fallback(self):
        primary, secondary = _Stub("主模型回复"), _Stub("备模型回复")
        chain = FallbackLLMClient([("a", primary), ("b", secondary)])
        assert chain.chat([{"role": "user", "content": "q"}]).content == "主模型回复"
        assert secondary.calls == 0

    def test_primary_down_falls_to_secondary(self):
        primary, secondary = _Stub(fail=True), _Stub("备模型回复")
        chain = FallbackLLMClient([("a", primary), ("b", secondary)])
        assert chain.chat([{"role": "user", "content": "q"}]).content == "备模型回复"
        assert chain.fallback_used == ["a:LLMError"]

    def test_all_down_lands_on_rule_template(self):
        chain = FallbackLLMClient([("a", _Stub(fail=True)), ("b", _Stub(fail=True))])
        resp = chain.chat([{"role": "user", "content": "q"}])
        assert resp.content == BUSY_TEMPLATE          # 永不抛异常的最后一环
        assert "转人工" in resp.content

    def test_structured_raises_when_all_down(self):
        chain = FallbackLLMClient([("a", _Stub(fail=True))])
        with pytest.raises(LLMError):
            chain.parse_structured([{"role": "user", "content": "x"}], dict)

    def test_fallback_logged_to_tracer(self):
        tracer = Tracer()
        chain = FallbackLLMClient([("a", _Stub(fail=True)), ("b", _Stub())],
                                  tracer=tracer)
        chain.chat([{"role": "user", "content": "q"}])
        assert any(e["type"] == "llm_fallback" for e in tracer.entries)

    def test_factory_offline_stays_mock(self):
        from app.llm.client import MockLLMClient
        s = Settings(mock_mode=True, fallback_models=["m2"])
        assert isinstance(build_client(s, Tracer()), MockLLMClient)

    def test_factory_real_with_fallbacks_builds_chain(self):
        s = Settings(mock_mode=False, openai_api_key="sk-x",
                     fallback_models=["m2"])
        client = build_client(s, Tracer())
        assert isinstance(client, FallbackLLMClient)


class TestCalibration:
    def test_calibrate_empty(self):
        from app.agent.rag.adaptive import calibrate
        assert calibrate([], "q") == 0.0

    def test_calibrate_range(self):
        from app.agent.rag.adaptive import calibrate
        from app.agent.rag.backends.base import RetrievedChunk
        from app.agent.rag.chunker import Chunk
        a = RetrievedChunk(score=1.0, chunk=Chunk("a", "d", "s",
                                                  "借款利率上限的法律规定原文"))
        b = RetrievedChunk(score=0.9, chunk=Chunk("b", "d", "s2", "无关内容"))
        v = calibrate([a, b], "借款利率上限")
        assert 0.0 < v <= 1.0


class TestThreeTierRefusal:
    def test_out_of_corpus_refuses(self, agent_settings):
        """语料外问题：低置信→空结果+转人工建议（承认失败而非硬答）。"""
        from app.agent.chat import LegalAgent
        agent = LegalAgent(agent_settings)
        try:
            out = json.loads(agent.tools.execute(
                "search_knowledge", {"query": "夸克颜色荷是什么物理概念"}))
            assert out["success"] is True
            assert out["chunks"] == []                 # refuse 档：不给依据不足的片段
            assert "转人工" in out["note"] or "未找到充分依据" in out["note"]
        finally:
            agent.close()

    def test_normal_query_keeps_chunks_and_confidence(self, agent_settings):
        from app.agent.chat import LegalAgent
        agent = LegalAgent(agent_settings)
        try:
            out = json.loads(agent.tools.execute(
                "search_knowledge", {"query": "交通事故责任如何划分"}))
            assert out["chunks"], "正常问题不应触发拒答档"
            assert 0 <= out.get("confidence", 0) <= 1
        finally:
            agent.close()
