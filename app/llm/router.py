"""模型故障转移链（FallbackLLMClient）：多模型依次降级 + 兜底模板。

生产等价物是 LiteLLM Router（多厂商路由/成本追踪/自动fallback）；
此处自有实现的教学价值在于把降级语义写透：
  主模型 → 备用模型 → 规则模板应答（"系统繁忙，已为您转人工"）
每级降级都进 tracer（可观测降级路径），最后一级永不抛异常——
调用方（ReAct循环）拿到模板应答即可优雅收尾，而不是崩溃。

置信度三档拒答（配套的兜错设计）在 tools/knowledge.py：
  confident(≥0.5) 正常返回 / low(≥0.25) 附"依据不足"标注 /
  refuse(<0.25) 空结果+建议转人工 —— 检索层承认失败比幻觉安全。
"""

from __future__ import annotations

from copy import copy
from typing import Optional

from app.agent.tracer import Tracer
from app.config.settings import Settings
from app.llm.client import BaseLLMClient, LLMError, LLMResponse, OpenAICompatClient

BUSY_TEMPLATE = ("系统当前繁忙，暂时无法完成本次分析。您的咨询已记录，"
                 "建议稍后重试或转人工律师（12348法律援助热线）。"
                 "涉及人身安全请立即拨打110。")


class RulesFallbackClient(BaseLLMClient):
    """链的最后一环：确定性模板应答，永不失败。"""

    def __init__(self, tracer: Optional[Tracer] = None):
        self._tracer = tracer

    def chat(self, messages, tools=None, temperature=None, max_tokens=None,
             purpose="react") -> LLMResponse:
        if self._tracer:
            self._tracer.log("llm_fallback_template", purpose=purpose)
        return LLMResponse(content=BUSY_TEMPLATE, prompt_tokens=0,
                           completion_tokens=len(BUSY_TEMPLATE) // 2)

    def parse_structured(self, messages, schema, purpose="extract"):
        raise LLMError("规则兜底层不支持结构化提取")


class FallbackLLMClient(BaseLLMClient):
    """依次尝试模型链；全部失败落规则模板（chat）或抛错（结构化提取）。

    models: [(model_name, client)]，按优先级排列。client 可为注入的
    测试替身（同 BaseLLMClient 接口）。
    """

    def __init__(self, chains: list[tuple[str, BaseLLMClient]],
                 tracer: Optional[Tracer] = None, tail: Optional[BaseLLMClient] = None):
        self._chains = chains
        self._tracer = tracer
        self._tail = tail or RulesFallbackClient(tracer)
        self.fallback_used: list[str] = []   # 记录实际发生的降级（诊断/测试）

    def chat(self, messages, tools=None, temperature=None, max_tokens=None,
             purpose="react") -> LLMResponse:
        for name, client in self._chains:
            try:
                return client.chat(messages, tools=tools, temperature=temperature,
                                   max_tokens=max_tokens, purpose=purpose)
            except LLMError as e:
                self.fallback_used.append(f"{name}:{type(e).__name__}")
                if self._tracer:
                    self._tracer.log("llm_fallback", from_model=name,
                                     purpose=purpose, error=str(e)[:120])
                continue
        return self._tail.chat(messages, tools=tools, temperature=temperature,
                               max_tokens=max_tokens, purpose=purpose)

    def parse_structured(self, messages, schema, purpose="extract"):
        last: Optional[LLMError] = None
        for name, client in self._chains:
            try:
                return client.parse_structured(messages, schema, purpose=purpose)
            except LLMError as e:
                self.fallback_used.append(f"{name}:parse")
                last = e
                continue
        raise last or LLMError("模型链全部不可用")


def build_fallback_client(settings: Settings, tracer: Tracer,
                          inner_clients: Optional[list[BaseLLMClient]] = None
                          ) -> Optional[FallbackLLMClient]:
    """工厂：fallback_models 非空且真实模式时返回链；否则 None（单模型直连）。

    inner_clients 供测试注入替身；生产路径按模型名各建一个 OpenAI 兼容客户端
    （共享 base_url/key，仅 model 不同——换厂商的异构路由是 LiteLLM 的场景）。
    """
    models = list(getattr(settings, "fallback_models", []) or [])
    models = [settings.model_name] + [m for m in models if m != settings.model_name]
    if len(models) < 2:
        return None
    if inner_clients is not None:
        chains = list(zip(models, inner_clients))
    else:
        chains = [(m, OpenAICompatClient(_with_model(settings, m), tracer))
                  for m in models]
    return FallbackLLMClient(chains, tracer=tracer)


def _with_model(settings: Settings, model: str) -> Settings:
    """浅拷贝配置并替换模型名（共享 key/base_url，仅换 model）。"""
    s2 = copy(settings)
    s2.model_name = model
    return s2
