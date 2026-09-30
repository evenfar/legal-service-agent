"""子 Agent 配置（第6期）：名字/提示词/工具白名单。

权限最小化的落地形态：每个子 Agent 只注册白名单内的工具
（registry.subset 生成视图），prompt 里再声明一遍 —— 双层约束。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.prompts.agents import (CONTRACT_PROMPT, EMERGENCY_PROMPT,
                                FAMILY_PROMPT, TORT_PROMPT)


@dataclass(frozen=True)
class SubAgentSpec:
    key: str
    name: str
    prompt: str
    tools: frozenset[str] = field(default_factory=frozenset)


SUBAGENT_SPECS: dict[str, SubAgentSpec] = {
    "family": SubAgentSpec(
        key="family", name="婚姻家事助理", prompt=FAMILY_PROMPT,
        tools=frozenset({"query_case", "query_law_article",
                         "search_knowledge", "recall_user_memory",
                         "load_skill"})),
    "contract": SubAgentSpec(
        key="contract", name="合同债务助理", prompt=CONTRACT_PROMPT,
        tools=frozenset({"query_case", "update_case_note",
                         "query_law_article", "search_precedents",
                         "calc_limitation", "query_court", "search_knowledge",
                         "recall_user_memory", "load_skill"})),
    "tort": SubAgentSpec(
        key="tort", name="侵权赔偿助理", prompt=TORT_PROMPT,
        tools=frozenset({"query_case", "query_law_article",
                         "search_precedents", "query_court",
                         "search_knowledge", "recall_user_memory"})),
    "emergency": SubAgentSpec(
        key="emergency", name="紧急通道助理", prompt=EMERGENCY_PROMPT,
        tools=frozenset()),
}
