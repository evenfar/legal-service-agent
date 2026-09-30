"""诉讼时效计算器 —— 法律场景最好的"确定性工具"。

为什么必须是代码而不是 LLM：日期算术要零错误（差一天可能丧失胜诉权）、
要可解释（给出起算规则与依据条文）、要瞬时返回。
这正是"确定性工作不交给模型"原则在法律域最典型的应用。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.agent.tools.mock_data import LIMITATION_RULES

WARNING_DAYS = 90


def calc_limitation(case_type: str, start_date: str) -> dict:
    """case_type 见 LIMITATION_RULES；start_date 为起算日（YYYY-MM-DD）。"""
    rule = LIMITATION_RULES.get(case_type)
    if not rule:
        return {"success": False,
                "error": f"不支持的案件类型「{case_type}」",
                "supported": list(LIMITATION_RULES.keys())}
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        return {"success": False,
                "error": "日期格式应为 YYYY-MM-DD，如 2024-03-15"}
    today = date.today()
    deadline = start + timedelta(days=round(rule["years"] * 365))
    days_left = (deadline - today).days
    if days_left < 0:
        status, advice = "已过时效", (
            "时效可能已届满，但仍有救济路径：确认是否存在中断/中止事由"
            "（催告、对方部分履行、不可抗力等），建议尽快咨询律师评估。")
        urgency = "urgent"
    elif days_left <= WARNING_DAYS:
        status, advice = "临近时效", (
            f"剩余不足{WARNING_DAYS}天！建议立即起诉立案，或采取中断措施"
            "（书面催告保留凭证、对方部分还款），中断后时效重新起算。")
        urgency = "urgent"
    elif days_left <= 180:
        status, advice = "时效尚可", "建议半年内启动准备（整理证据、起草诉状）。"
        urgency = "attention"
    else:
        status, advice = "时效充足", "可从容准备，注意保留期间内的沟通凭证。"
        urgency = "routine"
    display = rule.get("display_days")
    return {
        "success": True, "case_type": case_type,
        "start_date": str(start), "deadline": str(deadline),
        "days_left": days_left, "status": status, "urgency": urgency,
        "period_desc": f"{rule.get('display_days', str(rule['years']))}{'日' if display else '年'}",
        "basis": rule["basis"], "note": rule["note"], "advice": advice,
        "warning": days_left <= WARNING_DAYS,
    }
