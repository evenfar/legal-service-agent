"""MCP Server（法律域）：FastMCP + Streamable HTTP，独立微服务。

与 med 版同约定：只暴露只读工具；工具名与本地注册表完全同名
（同名覆盖，安全后处理按名匹配才不失效）；检索器惰性构建。
启动：python mcp_server/server.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from mcp.server.fastmcp import FastMCP  # mcp v1
except ModuleNotFoundError as e:
    raise ModuleNotFoundError(
        "本项目 MCP server 基于 mcp v1 API（FastMCP）。"
        "请安装 mcp>=1.8,<2（见 requirements.txt）") from e

from app.agent.rag.retriever import build_retriever  # noqa: E402
from app.agent.tools.case import query_case as _query_case  # noqa: E402
from app.agent.tools.court import query_court as _query_court  # noqa: E402
from app.agent.tools.law import query_law_article as _query_law_article  # noqa: E402
from app.agent.tools.limitation import calc_limitation as _calc_limitation  # noqa: E402
from app.agent.tools.precedents import search_precedents as _search_precedents  # noqa: E402
from app.config.settings import Settings  # noqa: E402

mcp = FastMCP("legal-tools", host="127.0.0.1", port=9302)
_settings = Settings()
_retriever = None


def _get_retriever():
    global _retriever
    if _retriever is None:
        _retriever = build_retriever(_settings)
    return _retriever


@mcp.tool()
def query_case(case_id: str) -> str:
    """查询案件进展（阶段/法院/下一步/关键日期）。案号格式 LS-2026-001。"""
    return json.dumps(_query_case(case_id), ensure_ascii=False)


@mcp.tool()
def query_law_article(query: str) -> str:
    """查高频法条（条号或主题词）。"""
    return json.dumps(_query_law_article(query), ensure_ascii=False)


@mcp.tool()
def search_precedents(query: str) -> str:
    """检索指导性案例裁判要点。"""
    return json.dumps(_search_precedents(query), ensure_ascii=False)


@mcp.tool()
def calc_limitation(case_type: str, start_date: str) -> str:
    """诉讼时效计算器。case_type: 一般民事/身体伤害索赔/劳动仲裁/交通事故索赔/行政复议。"""
    return json.dumps(_calc_limitation(case_type, start_date), ensure_ascii=False)


@mcp.tool()
def query_court(keyword: str) -> str:
    """查询法院管辖与立案方式。"""
    return json.dumps(_query_court(keyword), ensure_ascii=False)


@mcp.tool()
def search_knowledge(query: str) -> str:
    """检索法律知识库（民法典/指导案例/司法解释），返回带来源片段。"""
    try:
        chunks = _get_retriever().search(query, top_k=3)
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
    results = [{"source": f"{c.chunk.doc}#{c.chunk.section}", "text": c.chunk.text}
               for c in chunks]
    return json.dumps({"success": True, "query": query, "chunks": results},
                      ensure_ascii=False)


if __name__ == "__main__":
    print(f"MCP server 启动: http://127.0.0.1:9302/mcp "
          f"(offline={not _settings.openai_api_key})")
    mcp.run(transport="streamable-http")
