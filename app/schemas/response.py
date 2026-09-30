"""结构化输出 schema（法律域）：LegalResponse。

与医疗版同构的设计：受控枚举 + 确定性布尔。法律场景的特有映射：
intent 覆盖案件/法条/类案/程序四类咨询 + 紧急红线；
requires_human = 转人工律师/法律援助；
urgency 的"紧急"对应时效/证据风险，"危急"对应正在发生的人身威胁。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class IntentType(str, Enum):
    greeting = "greeting"          # 问候
    case_query = "case_query"      # 案件进展查询
    law_article = "law_article"    # 法条咨询
    precedent = "precedent"        # 类案参考
    procedure = "procedure"        # 程序/时效/管辖咨询
    emergency = "emergency"        # 紧急红线（人身安全/证据灭失/时效临界）
    other = "other"


class UrgencyLevel(str, Enum):
    routine = "routine"      # 常规咨询
    attention = "attention"  # 需留意（如时效尚余半年）
    urgent = "urgent"        # 紧迫（时效临近/证据有灭失风险）
    emergency = "emergency"  # 人身安全正在受威胁


class LegalResponse(BaseModel):
    """每轮对话的最终结构化产出。"""

    intent: IntentType = Field(description="意图分类", default=IntentType.other)
    confidence: float = Field(ge=0.0, le=1.0, description="置信度", default=0.8)
    reply: str = Field(min_length=1, description="给用户的回复正文")
    requires_human: bool = Field(default=False,
                                 description="是否需要转人工律师/法律援助")
    urgency: UrgencyLevel = Field(default=UrgencyLevel.routine, description="紧迫程度")
    follow_up_question: Optional[str] = Field(default=None, description="可选的追问")


INTENT_LABELS = {
    IntentType.greeting: "问候",
    IntentType.case_query: "案件查询",
    IntentType.law_article: "法条咨询",
    IntentType.precedent: "类案参考",
    IntentType.procedure: "程序咨询",
    IntentType.emergency: "紧急红线",
    IntentType.other: "其他",
}
