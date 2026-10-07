"""图状态定义：LangGraph 的 State 即手写版各方法间传递的隐式上下文。

手写版把"一轮对话的中间产物"挂在 self.raw_messages / self.summary 等实例属性上；
LangGraph 把它们收敛成一个显式 state dict，节点只读它、返回局部更新（框架合并）。
对应关系：messages↔raw_messages、final/used_tools/outputs↔react()返回的三元组、
redflag_response↔handle_redflag()的返回、forced_human↔safety_post_process()的标记。
"""

from __future__ import annotations

from typing import Optional, TypedDict

from app.schemas.response import LegalResponse


class GraphState(TypedDict, total=False):
    """一轮咨询经过图管道的全部状态。total=False：节点可返回部分更新。"""

    messages: list                     # OpenAI 风格消息列表（含 system/工具轨迹）
    user_input: str                    # 本轮用户输入
    final: str                         # ReAct 产出的最终文本
    used_tools: list                   # 本轮调用过的工具名（= react() 的 used）
    outputs: list                      # [(工具名, 原始输出JSON)]（= react() 的 outputs）
    redflag_response: Optional[LegalResponse]  # 红线命中的急诊应答（条件边依据）
    response: Optional[dict]           # 最终 LegalResponse 的 dict 形式
    forced_human: bool                 # 安全后处理强制转人工标记
