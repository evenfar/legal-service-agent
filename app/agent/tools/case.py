"""案件查询工具。"""

from __future__ import annotations

from app.agent.tools.mock_data import CASES


def query_case(case_id: str) -> dict:
    c = CASES.get(case_id)
    if not c:
        return {"success": False,
                "error": f"未找到案件 {case_id}，请核实案号（格式如 LS-2026-001）"}
    return {"success": True, "case": c}


def update_case_note(case_id: str, note: str) -> dict:
    """当事人补充备注（写操作示例：真实修改案件记录）。"""
    c = CASES.get(case_id)
    if not c:
        return {"success": False, "error": f"未找到案件 {case_id}"}
    c.setdefault("notes", []).append(note)
    return {"success": True, "message": f"已为案件 {case_id} 添加备注",
            "total_notes": len(c["notes"])}
