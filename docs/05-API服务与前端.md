# 05 · API 服务与前端

第 6 块工程拼图：把 CLI 版 `main.py` 的 LegalAgent 搬到浏览器——
FastAPI 服务层 + 单文件聊天前端 + SSE 实时思考流。agent 栈一行未改。

## 启动

```bash
uvicorn app.api:app --port 8300
# 浏览器打开 http://127.0.0.1:8300
```

默认 **mock 模式**（零成本演示）；前端切到 `real` 或请求带 `?mode=real`
时走 `.env` 配置的真实 LLM（前端有确认提示）。会话文件落在
`app/sessions/api/{session_id}.json`（该目录已 gitignore）。

界面预览：`docs/screenshot-api-demo.png`（左聊天气泡 + 右思考过程事件流）。

## 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | `{status, offline, sessions}`；offline 反映服务器环境是否具备真实模式条件 |
| `/chat` | POST | body `{session_id, message, mode?}` → `{reply, intent, confidence, urgency, requires_human, follow_up_question, events}`；events 为本轮 tracer 增量（白名单过滤） |
| `/chat/stream` | GET | 同 `/chat` 的 SSE 版，参数走 query（`session_id/message/mode`） |
| `/reset` | POST | body `{session_id, mode?}`：关闭旧 agent（触发记忆巩固）并重建 |
| `/` `/static/*` | GET | 聊天前端与静态资源；`/docs/*` 直接伺服 docs 目录（chain-explorer 可从页面直链） |

`session_id` 由前端生成（uuid），服务端以内存 dict `{session_id → agent}` +
`threading.Lock` 管理；模式（mock/real）变化时自动重建 agent。

## SSE 事件协议

`Content-Type: text/event-stream`，事件白名单（`app/api.py: STREAM_EVENT_TYPES`）：

| 事件 | 载荷要点 | 含义 |
|---|---|---|
| `llm_call` | purpose / model / prompt+completion tokens / latency | 一次 LLM 调用（react、extract、router 等） |
| `tool_call` | tool / arguments / ok / latency_ms | ReAct 工具调用 |
| `route` | agent | 多 Agent 分流结果 |
| `redflag_shortcut` | flags | 人身安全红线规则短路（不进 LLM） |
| `nli_check` | checked / supported / supported_ratio | 回复-证据句级核验 |
| `llm_fallback` | 链信息 | 主模型失败降级 |
| `injection_blocked` | original_len | 出站注入扫描拦截 |

- 白名单事件：`data: {JSON}\n\n`（默认 message 事件）；
- 结束：`event: done\ndata: {LegalResponse JSON}\n\n`；
- 失败：`event: error\ndata: {"detail": "..."}\n\n`。

实现方式：`agent.chat()` 在后台线程执行，SSE 生成器每 80ms 轮询
`tracer.cursor` 取增量（tracer 自带锁，跨线程安全），线程结束后冲刷剩余
事件再发 done——mock 模式同样有完整事件流，"看 agent 思考"零成本可演示。

## 架构

```
浏览器( static/index.html )
   │  fetch + ReadableStream 解析 SSE
   ▼
FastAPI ( app/api.py )  —— 同步 endpoint，线程池执行
   │  get_agent(session_id, settings) 工厂
   ▼
LegalAgent / MultiAgentOrchestrator（与 CLI 完全同一套）
   ├─ 工具（案件/法条/类案/时效/法院/技能/MCP）
   ├─ RAG（local/chroma + 自适应栈）
   ├─ 记忆（短期 + 长期）
   └─ Tracer ──(轮询 cursor)──► SSE 事件流 ──► 右侧"思考过程"面板
```

## 与 CLI / chain-explorer 的关系

- **CLI（main.py）**：交互式终端，同一 agent 栈的最早入口；
- **本服务**：浏览器可视化入口，面向演示与联调；
- **chain-explorer.html**：静态架构讲解（四 Tab），从本页面右上角可直达——
  一个讲"系统长什么样"，一个演"系统正在怎么跑"。
