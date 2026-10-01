"""API 服务层：FastAPI + SSE，把 CLI 版 LegalAgent 搬到浏览器。

工程要点：
- 同步风格（def 而非 async def）：agent 栈（LLM client/工具/记忆）全是同步阻塞的，
  FastAPI 会自动把同步 endpoint 丢进线程池，不会卡住事件循环。
- 会话管理：内存 dict {session_id → agent} + 一把锁；session_id 由前端生成（uuid），
  每个会话独立 session 文件（app/sessions/api/），互不串扰。
- 默认 mock 模式（零成本演示）；?mode=real 按请求切换真实 LLM。
- 事件流：chat 在后台线程跑，SSE 生成器轮询 tracer.cursor（80ms）把新增事件
  实时推给前端——"看 agent 思考"是本演示的核心亮点，mock 下 tracer 照记。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config.settings import Settings, is_offline

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DOCS_DIR = BASE_DIR / "docs"

# SSE / POST /chat 对外暴露的事件白名单：只推"思考过程"相关的可读事件
STREAM_EVENT_TYPES = {"llm_call", "tool_call", "route", "redflag_shortcut",
                      "nli_check", "llm_fallback", "injection_blocked"}
POLL_INTERVAL = 0.08  # 生成器轮询 tracer 的间隔（秒）

# 服务端基础配置（真实模式条件、multi-agent 开关等从这里继承）；
# 测试用 monkeypatch 替换成 tmp 路径的离线 Settings。
BASE_SETTINGS = Settings()

SESSIONS: dict[str, object] = {}          # session_id → agent
SESSION_MODES: dict[str, bool] = {}       # session_id → 当前 mock 与否
LOCK = threading.Lock()

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------- 会话工厂（项目注入风格） ----------------

def build_settings(mode: Optional[str]) -> Settings:
    """mode=real 用环境配置的真实模型；其余（默认/未知）强制 mock。"""
    data = BASE_SETTINGS.model_dump()
    data["mock_mode"] = (mode or "mock").lower() != "real"
    return Settings(**data)


def _session_file(settings: Settings, session_id: str) -> str:
    """每个 API 会话一个独立文件，落在 settings.session_path 同级目录（可被测试隔离）。"""
    d = os.path.dirname(settings.session_path) or "."
    return os.path.join(d, "api", f"{session_id}.json")


def build_agent(session_id: str, settings: Settings):
    """按 settings 选择单 Agent / 编排器（与 main.py 同款装配，仅会话路径不同）。"""
    if settings.multi_agent_enabled:
        from app.multi_agent.orchestrator import MultiAgentOrchestrator
        return MultiAgentOrchestrator(settings,
                                      session_path=_session_file(settings, session_id))
    from app.agent.chat import LegalAgent
    return LegalAgent(settings, session_path=_session_file(settings, session_id))


def get_agent(session_id: str, settings: Settings):
    """获取或创建会话；模式变化时关闭旧 agent 重建（锁内完成，调用方勿长持锁）。"""
    mock = settings.mock_mode
    with LOCK:
        if session_id in SESSIONS and SESSION_MODES.get(session_id) == mock:
            return SESSIONS[session_id]
        old = SESSIONS.pop(session_id, None)
        SESSION_MODES.pop(session_id, None)
        if old is not None:
            _safe_close(old)
        agent = build_agent(session_id, settings)
        SESSIONS[session_id] = agent
        SESSION_MODES[session_id] = mock
        return agent


def _safe_close(agent) -> None:
    """关闭单个 agent：memory 巩固会调 LLM，mock 下无害；真实模式可能慢，绝不抛。"""
    try:
        agent.close()
    except Exception:  # noqa: BLE001 —— 关闭失败只丢弃会话，不影响服务
        pass


def _validate_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(status_code=400,
                            detail="session_id 需为 1~64 位字母/数字/连字符/下划线")
    return session_id


def _filtered(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") in STREAM_EVENT_TYPES]


# ---------------- 应用与生命周期 ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    SESSIONS.clear()
    SESSION_MODES.clear()
    yield
    with LOCK:
        agents = list(SESSIONS.values())
        SESSIONS.clear()
        SESSION_MODES.clear()
    for agent in agents:      # 锁外逐个关：close 可能触发 LLM 巩固（见 _safe_close）
        _safe_close(agent)


app = FastAPI(title="法律咨询 Agent API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])  # 本地演示，放开跨域
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/docs", StaticFiles(directory=str(DOCS_DIR)), name="docs")  # chain-explorer 直链


class ChatRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1)
    mode: Optional[str] = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "offline": is_offline(BASE_SETTINGS),
            "sessions": len(SESSIONS)}


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    _validate_session_id(req.session_id)
    settings = build_settings(req.mode)
    agent = get_agent(req.session_id, settings)
    cursor = agent.tracer.cursor
    resp = agent.chat(req.message)  # 同步 endpoint：FastAPI 线程池执行
    return {"reply": resp.reply, "intent": resp.intent.value,
            "confidence": resp.confidence, "urgency": resp.urgency.value,
            "requires_human": resp.requires_human,
            "follow_up_question": resp.follow_up_question,
            "events": _filtered(agent.tracer.since(cursor))}


@app.get("/chat/stream")
def chat_stream(session_id: str, message: str, mode: Optional[str] = None):
    """SSE 流式对话：事件白名单逐条推送，最后 event:done 携带完整 LegalResponse。"""
    _validate_session_id(session_id)
    settings = build_settings(mode)
    agent = get_agent(session_id, settings)

    box: dict = {}

    def _run():
        try:
            box["resp"] = agent.chat(message)
        except Exception as e:  # noqa: BLE001 —— 线程内异常经 box 传回生成器
            box["error"] = str(e)

    worker = threading.Thread(target=_run, daemon=True)
    cursor = agent.tracer.cursor  # 线程启动前先记起点：mock 下 chat 毫秒级即完成
    worker.start()

    def gen():
        nonlocal cursor
        yield "retry: 3000\n\n"
        while True:
            batch = agent.tracer.since(cursor)
            for e in _filtered(batch):
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            cursor += len(batch)   # 按实际取到的条数推进，不漏不重
            if not worker.is_alive():
                for e in _filtered(agent.tracer.since(cursor)):  # 冲刷收尾事件
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                break
            time.sleep(POLL_INTERVAL)
        worker.join()
        if "error" in box:
            yield (f"event: error\ndata: "
                   f"{json.dumps({'detail': box['error']}, ensure_ascii=False)}\n\n")
            return
        payload = json.dumps(box["resp"].model_dump(mode="json"), ensure_ascii=False)
        yield f"event: done\ndata: {payload}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class ResetRequest(BaseModel):
    session_id: str
    mode: Optional[str] = None


@app.post("/reset")
def reset(req: ResetRequest) -> dict:
    _validate_session_id(req.session_id)
    settings = build_settings(req.mode)
    with LOCK:
        old = SESSIONS.pop(req.session_id, None)
        SESSION_MODES.pop(req.session_id, None)
    if old is not None:
        _safe_close(old)
    get_agent(req.session_id, settings)  # 立即重建：sessions 数不增
    return {"status": "reset", "session_id": req.session_id}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
