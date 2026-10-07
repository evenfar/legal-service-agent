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

## 学习模式（static/index.html，零后端改动）

前端在聊天界面之上叠加了四个教学组件，全部为纯原生 JS/CSS、无外部依赖，
mock 模式即可完整演示：

1. **实时管线条**（顶栏下方）：七个阶段节点
   `①红旗短路 → ②上下文组装 → ③ReAct循环 → ④安全后处理 → ⑤结构化提取
   → ⑥记忆压缩 → ⑦持久化`，随 SSE 事件点亮（`redflag_shortcut→①`、
   `llm_call(purpose含react)/tool_call→③`（带🔧N计数与闪烁）、
   `nli_check/injection_blocked→④`；收到 done 后 ⑤⑥⑦ 以 200ms 间隔级联点亮）。
   若本轮只有红旗短路而无任何 llm_call，③④ 保持暗灰并加盖
   「被①短路」标签——直观呈现"零 LLM 急症短路"。每轮新消息发送即重置；
   悬停节点可看该阶段职责说明。
2. **事件教学注解**：右侧事件卡在 mono 详情行之上新增一行浅色小字，
   按知识映射表解释事件含义（如 tool_call →
   "模型自主决定调用 {tool}——执行结果将作为[观察结果]回填对话"），
   原始 JSON 折叠在卡片底部可展开。
3. **引导实验**（聊天区顶部可折叠卡片）：六个预设剧本
   （查案件两段式 ReAct / 急症红线零 LLM / 时效计算 / 法条条号归一化 /
   RAG 检索 / 先查后写链式调用）。点击即展示观察点清单并自动发送；
   done 后基于本轮 events 与响应做客户端核对，逐条渲染 ✓/✗，
   全部命中时卡片变绿并标「🎯全部命中」。
4. **本轮指标 + 复盘重播**：事件面板底部固定小结行
   `本轮: {n}次LLM · {m}次工具 · ~{tokens}tokens`（llm_call 的
   prompt+completion 求和）。每个完成的气泡角落有「复盘▶」按钮：
   进入复盘模式后右侧面板清空并按 300ms/条重播本轮事件（管线同步点亮、
   JSON 详情自动展开），底部提供 ⏮◀▶⏭ 与退出按钮（亦支持
   ←/→/空格/Esc 快捷键），复盘期间禁用发送。

安全与稳定性约束：所有用户/API 数据一律 `textContent` 渲染（事件参数
可能含尖括号）；SSE 错误/断流路径保留已点亮的管线与指标、不产生复盘
按钮；重置会话会同步清空学习 UI。截图示例：
`docs/screenshot-learn-redflag.png`（急症红线实验）、
`docs/screenshot-learn-case.png`（查案件实验 + 事件注解）。
