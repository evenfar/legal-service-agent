"""POST /chat 性能基准（零第三方依赖：http.client + threading 纯 stdlib）。

测量对象：mock 模式下一次完整编排（红旗检查→ReAct→安全→提取→记忆→持久化）
的服务端延迟与吞吐，用于量级感知，不是专业压测（结论与边界见
docs/07-性能基准.md）。

用法：
  python -m app.scripts.bench_api --url http://127.0.0.1:8310 \
      --concurrency 1,4,16 --rounds 50 --warmup 5

两个测量口径（读数字前必看）：
- **每个请求独立 session_id**：服务端对同一 session_id 的并发请求用
  _session_lock 串行化——同 session 串行是**设计行为**（保护会话历史/记忆
  一致性），复用同一 id 压测测到的是锁排队而非编排能力。独立 id 的代价是
  每个请求都冷装配一个 agent（检索器/技能/记忆），数字含这部分开销，
  等于"全是新会话首问"的最重口径。session_id 带运行标签（时间戳），
  对同一服务重复压测也始终命中新会话，不会退化成"老会话续聊"。
- **每请求新建 TCP 连接**（Connection: close，不用 keep-alive），
  与浏览器首次请求的保守口径一致。
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import threading
import time
import urllib.parse
from datetime import datetime

DEFAULT_MESSAGE = "2023年6月借的钱时效还剩多久"  # 典型路径：时效工具 + RAG 检索


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="POST /chat（mock）性能基准")
    p.add_argument("--url", default="http://127.0.0.1:8300")
    p.add_argument("--concurrency", default="1,4,16",
                   help="逗号分隔的并发档位列表")
    p.add_argument("--rounds", type=int, default=50,
                   help="每个工作线程的请求数（总请求数 = 并发 × rounds）")
    p.add_argument("--warmup", type=int, default=5,
                   help="每档正式计时前的预热请求数（不计入统计）")
    p.add_argument("--message", default=DEFAULT_MESSAGE)
    p.add_argument("--timeout", type=float, default=60.0)
    return p.parse_args()


def one_request(host: str, port: int, prefix: str, session_id: str,
                message: str, timeout: float) -> tuple[float, bool]:
    """单次请求：新建连接 → POST /chat → 读完整个响应。返回 (耗时秒, 成功否)。

    HTTPConnection 在 .request() 时才建 TCP 连接，计时起点覆盖连接建立；
    成功判定 = HTTP 200 且响应体能解析为 JSON。
    """
    body = json.dumps({"session_id": session_id, "message": message})
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    start = time.perf_counter()
    try:
        conn.request("POST", f"{prefix}/chat", body=body,
                     headers={"Content-Type": "application/json",
                              "Connection": "close"})
        resp = conn.getresponse()
        raw = resp.read()
        ok = resp.status == 200
        if ok:
            json.loads(raw)  # 校验完整可解析，防"半截响应也算成功"
        return time.perf_counter() - start, ok
    except Exception:  # noqa: BLE001 —— 连接失败/超时/坏响应统一计为错误
        return time.perf_counter() - start, False
    finally:
        conn.close()


def pct(sorted_lat: list[float], p: float) -> float:
    """最近邻法百分位（P50 of n=50 → 第25个），无样本返回 nan。"""
    if not sorted_lat:
        return float("nan")
    idx = math.ceil(p / 100 * len(sorted_lat))
    return sorted_lat[min(idx, len(sorted_lat)) - 1]


def _worker(tid: int, tag: str, rounds: int, host: str, port: int,
            prefix: str, message: str, timeout: float,
            barrier: threading.Barrier, out: list[list[tuple[float, bool]]]) -> None:
    barrier.wait()  # 全线程对齐起跑，避免先到的线程独占低噪声窗口
    for i in range(rounds):
        # 独立 session_id（同 session 串行是服务端设计行为，见模块注释）
        dt, ok = one_request(host, port, prefix, f"{tag}-t{tid}-r{i}",
                             message, timeout)
        out[tid].append((dt, ok))


def run_level(host: str, port: int, prefix: str, level: int, rounds: int,
              warmup: int, message: str, timeout: float,
              run_tag: str) -> str:
    """跑一个并发档，返回一行结果（延迟取成功请求，QPS 按全部请求算）。"""
    tag = f"bench-{run_tag}-c{level}"
    for w in range(warmup):  # 预热：触发服务端首次导入/索引装配，不计时
        one_request(host, port, prefix, f"{tag}-warm-{w}", message, timeout)

    out: list[list[tuple[float, bool]]] = [[] for _ in range(level)]
    barrier = threading.Barrier(level + 1)
    threads = [threading.Thread(target=_worker,
                                args=(t, tag, rounds, host, port, prefix,
                                      message, timeout, barrier, out))
               for t in range(level)]
    for t in threads:
        t.start()
    barrier.wait()          # 主线程与全部 worker 同点放行
    start = time.perf_counter()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    flat = [x for lst in out for x in lst]
    lats = sorted(dt for dt, ok in flat if ok)
    errors = sum(1 for _, ok in flat if not ok)
    mean = sum(lats) / len(lats) if lats else float("nan")
    qps = len(flat) / elapsed if elapsed > 0 else float("nan")
    cells = [f"{level:>4}", f"{len(flat):>6}",
             *(f"{pct(lats, p) * 1000:8.1f}" for p in (50, 90, 95, 99)),
             f"{mean * 1000:8.1f}", f"{qps:7.1f}", f"{errors:>4}"]
    return " | ".join(cells)


def main() -> None:
    args = parse_args()
    u = urllib.parse.urlparse(args.url if "://" in args.url else "http://" + args.url)
    host, port = u.hostname or "127.0.0.1", u.port or 80
    prefix = (u.path or "").rstrip("/")

    conn = http.client.HTTPConnection(host, port, timeout=args.timeout)  # 预检
    try:
        conn.request("GET", f"{prefix}/health")
        if conn.getresponse().status != 200:
            raise ConnectionError("/health 非 200")
    except Exception as exc:  # noqa: BLE001 —— 给出可操作的失败信息
        raise SystemExit(f"服务不可达 {args.url}（{exc}）——先启动: "
                         "uvicorn app.api:app --port 8310") from exc
    finally:
        conn.close()

    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    run_tag = f"{time.time_ns() // 1_000_000 % 10 ** 9:x}"  # 本次运行标签
    print(f"# bench_api  {datetime.now():%Y-%m-%d %H:%M}  url={args.url}"
          f"  rounds/线程={args.rounds}  warmup={args.warmup}")
    print(f"# message={args.message!r}  独立session/请求 + 每请求新建连接（见模块注释）")
    print(f"{'并发':>4} | {'请求数':>6} | {'P50/ms':>8} | {'P90/ms':>8} | "
          f"{'P95/ms':>8} | {'P99/ms':>8} | {'均值/ms':>8} | {'QPS':>7} | {'错误':>4}")
    print("-" * 84)
    for level in levels:
        print(run_level(host, port, prefix, level, args.rounds,
                        args.warmup, args.message, args.timeout, run_tag))
    print("-" * 84)
    print("# 延迟列只统计成功请求；QPS=总请求数/墙钟时间。mock 模式数字 ="
          " 编排层开销，解读见 docs/07-性能基准.md")


if __name__ == "__main__":
    main()
