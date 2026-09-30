"""管辖/立案信息工具。"""

from __future__ import annotations

from app.agent.tools.mock_data import COURTS


def query_court(keyword: str) -> dict:
    for key, info in COURTS.items():
        if keyword and (keyword in info["name"] or keyword in info["scope"]
                        or keyword in info["tips"] or keyword == key):
            return {"success": True, "court": info}
    return {"success": False,
            "error": f"未匹配到「{keyword}」的管辖信息",
            "available": list(COURTS.keys())}
