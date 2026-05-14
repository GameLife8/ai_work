"""ai-ops-agent — 每个节点上跑的 HTTP 诊断代理。

设计原则
========
1. **单进程一文件**：~300 行 Python，启动 1s 内能响应。
2. **HTTP-only 协议**：平台 backend → ``http://<node>:<port>/v1/exec``，
   不再走 ``docker exec``，从而**彻底摆脱** per-node TCP daemon 暴露的需求。
3. **命令白名单**：``allowed.yml`` 配的命令才能被调用；任何 shell 注入都打不穿
   "数组形式 + 白名单 + 子进程 exec（无 shell）" 这层防线。
4. **nsenter 自动注入**：调用方只传业务命令（``["ss","-ltnp"]``），agent 自己
   拼 ``nsenter -t 1 -m -u -i -n -p -- <cmd>``，进宿主机 namespace 执行。
5. **流式输出**：``/v1/exec/stream`` 给 ``tcpdump``/``tail -f`` 这种长命令用 SSE。

API
===

``GET  /livez``        无鉴权探活（docker/k8s healthcheck 用）
``GET  /v1/health``    有鉴权，返回详细状态
``POST /v1/exec``      JSON in/out，一次性命令
``POST /v1/exec/stream`` JSON in / SSE out，流式

请求体格式（``/v1/exec`` 和 ``/v1/exec/stream`` 共用）::

    {
      "cmd":              ["ss", "-ltnp"],     # 必填，数组形式
      "nsenter":          "muinp",              # 可选，默认 "muinp"
                                                # m=mount u=uts i=ipc n=net p=pid
                                                # 传 "" 表示**不**进 host ns（容器内执行）
      "timeout_sec":      30,                   # 默认 30，上限 300
      "max_output_bytes": 1048576               # 默认 1MB，上限 10MB
    }

响应（``/v1/exec``）::

    {
      "exit_code":   0,
      "stdout":      "...",
      "stderr":      "...",
      "duration_ms": 87,
      "truncated":   false,
      "timeout":     false        # 超时为 true，exit_code = -1
    }

响应（``/v1/exec/stream``）—— SSE 事件流::

    event: stdout
    data: 22:14:50.123 IP 10.0.0.1.443 > 10.0.5.6.41252: Flags [S.]

    event: stderr
    data: tcpdump: listening on any...

    event: exit
    data: {"exit_code":0,"duration_ms":30001,"timeout":false}

错误情况都返回 4xx/5xx + ``{"error": "..."}``。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path

import yaml
from aiohttp import web


AGENT_VERSION = "1.0.0"

# ---------- 配置 ----------

PORT = int(os.getenv("AGENT_PORT", "9100"))
TOKEN_FILE = os.getenv("AGENT_AUTH_TOKEN_FILE", "/run/secrets/token")
TOKEN_ENV = os.getenv("AGENT_AUTH_TOKEN", "")          # 仅 dev/测试用；生产请用 file
ALLOWED_CIDR = os.getenv("AGENT_ALLOWED_CIDR", "")     # 逗号分隔；空 = 不做 IP 限制
ALLOW_FILE = os.getenv("AGENT_ALLOWED_FILE", "/etc/ai-ops-agent/allowed.yml")
NODE_NAME = os.getenv("AGENT_NODE_NAME") or socket.gethostname()

# 全局上限——单次调用兜底
MAX_TIMEOUT_SEC = 300
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_OUTPUT_BYTES = 1024 * 1024

# 运行时状态（不需要原子操作；GIL 已经保证 int +1 原子）
_state: dict[str, object] = {
    "token": "",
    "allowed_commands": set(),
    "start_time": time.time(),
    "exec_count": 0,
}

logger = logging.getLogger("ai-ops-agent")


# ---------- 启动配置加载 ----------

def load_config() -> None:
    """读 token + 白名单。任一缺失都直接退出——agent 没法在不安全状态下运行。"""

    # token：优先文件（secret 挂载），其次环境变量
    token = ""
    token_path = Path(TOKEN_FILE)
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
    elif TOKEN_ENV:
        token = TOKEN_ENV.strip()
    if not token:
        raise RuntimeError(
            f"agent 未配置 token：既没读到 {TOKEN_FILE}，也没设 AGENT_AUTH_TOKEN 环境变量"
        )
    _state["token"] = token

    # 白名单
    allow_path = Path(ALLOW_FILE)
    if not allow_path.exists():
        raise RuntimeError(f"agent 找不到白名单文件 {ALLOW_FILE}")
    with allow_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cmds = set(data.get("read_only") or [])
    if not cmds:
        raise RuntimeError(f"{ALLOW_FILE} 的 read_only 列表为空——拒绝启动")
    _state["allowed_commands"] = cmds

    logger.info(
        "agent 启动配置就绪：node=%s port=%s allowed=%s",
        NODE_NAME, PORT, sorted(cmds),
    )


# ---------- 鉴权中间件 ----------

@web.middleware
async def auth_mw(request: web.Request, handler):
    # /livez 不走鉴权，给 docker/k8s healthcheck 用
    if request.path == "/livez":
        return await handler(request)

    # 1. 来源 CIDR 白名单（可选）
    if ALLOWED_CIDR:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        peer_ip = peer[0] if peer else ""
        if not _ip_in_cidrs(peer_ip, ALLOWED_CIDR):
            logger.warning("拒绝来源 %s 不在 ALLOWED_CIDR=%s", peer_ip, ALLOWED_CIDR)
            return web.json_response(
                {"error": "source_forbidden", "peer_ip": peer_ip}, status=403,
            )

    # 2. Bearer token
    authz = request.headers.get("Authorization", "")
    expected = _state["token"]
    if not authz.startswith("Bearer ") or authz[7:].strip() != expected:
        return web.json_response({"error": "unauthorized"}, status=401)

    return await handler(request)


def _ip_in_cidrs(ip: str, cidrs_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for c in cidrs_str.split(","):
        c = c.strip()
        if not c:
            continue
        try:
            if ip_obj in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


# ---------- 命令组装 + 校验 ----------

# nsenter 单字母 → 完整 flag
_NS_FLAGS = {
    "m": "--mount", "u": "--uts", "i": "--ipc",
    "n": "--net",   "p": "--pid",  "U": "--user",
    "C": "--cgroup",
}


def _build_argv(payload: dict) -> tuple[list[str], float, int]:
    """从请求体造最终 argv（含 nsenter wrap）+ timeout + max_output_bytes。

    抛 ``ValueError`` 表示请求格式错（→ 400）；
    抛 ``PermissionError`` 表示命令不在白名单（→ 403）。
    """

    cmd = payload.get("cmd")
    if not isinstance(cmd, list) or not cmd:
        raise ValueError("cmd 必须是非空数组")
    if not all(isinstance(s, str) for s in cmd):
        raise ValueError("cmd 元素必须全是字符串")

    # 取 basename 做白名单匹配，允许调用方传 /usr/bin/ss 或者 ss
    binary = cmd[0]
    base = binary.rsplit("/", 1)[-1]
    allowed = _state["allowed_commands"]
    if base not in allowed:
        raise PermissionError(
            f"命令 {base!r} 不在白名单 ({sorted(allowed)})——"
            "如需新增请改 /etc/ai-ops-agent/allowed.yml 并重启 agent"
        )
    # 用户不应该自己塞 nsenter——agent 会自动包一层；防止重复包破坏语义
    if base in {"nsenter", "unshare"}:
        raise PermissionError(
            f"不要直接调用 {base}；agent 会自动用 nsenter 包裹业务命令"
        )

    # nsenter 配置：默认进 m/u/i/n/p；显式传 "" 表示在 agent 容器自己的 ns 里跑
    ns_str = payload.get("nsenter")
    if ns_str is None:
        ns_str = "muinp"
    if not isinstance(ns_str, str):
        raise ValueError("nsenter 必须是字符串，如 'muinp' 或 ''")

    if ns_str:
        argv = ["nsenter", "-t", "1"]
        for ch in ns_str:
            flag = _NS_FLAGS.get(ch)
            if not flag:
                raise ValueError(f"nsenter 字符 {ch!r} 未识别")
            argv.append(flag)
        argv.append("--")
        argv.extend(cmd)
    else:
        argv = list(cmd)

    # 限流参数
    timeout = float(payload.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
    if timeout <= 0:
        raise ValueError("timeout_sec 必须 > 0")
    timeout = min(timeout, MAX_TIMEOUT_SEC)

    max_bytes = int(payload.get("max_output_bytes") or DEFAULT_OUTPUT_BYTES)
    if max_bytes <= 0:
        raise ValueError("max_output_bytes 必须 > 0")
    max_bytes = min(max_bytes, MAX_OUTPUT_BYTES)

    return argv, timeout, max_bytes


# ---------- 路由 handlers ----------

async def livez(_request: web.Request) -> web.Response:
    """无鉴权探活——给 healthcheck 用，不带任何敏感信息。"""
    return web.Response(text="ok", content_type="text/plain")


async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "node": NODE_NAME,
        "agent_version": AGENT_VERSION,
        "uptime_seconds": int(time.time() - _state["start_time"]),
        "exec_count": _state["exec_count"],
        "allowed_commands": sorted(_state["allowed_commands"]),
        "limits": {
            "max_timeout_sec": MAX_TIMEOUT_SEC,
            "max_output_bytes": MAX_OUTPUT_BYTES,
        },
    })


async def exec_oneshot(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid_json"}, status=400)

    try:
        argv, timeout, max_bytes = _build_argv(payload)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    _state["exec_count"] += 1
    started = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return web.json_response(
            {"error": f"binary_not_found: {exc}"},
            status=500,
        )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # 超时——先 SIGTERM，没死再 SIGKILL
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
        return web.json_response({
            "exit_code": -1,
            "stdout": "",
            "stderr": f"command timeout after {timeout}s",
            "duration_ms": int((time.time() - started) * 1000),
            "truncated": False,
            "timeout": True,
        })

    sb = stdout or b""
    se = stderr or b""
    truncated = False
    if len(sb) > max_bytes:
        sb = sb[:max_bytes]
        truncated = True
    if len(se) > max_bytes:
        se = se[:max_bytes]
        truncated = True

    return web.json_response({
        "exit_code": proc.returncode,
        "stdout": sb.decode("utf-8", errors="replace"),
        "stderr": se.decode("utf-8", errors="replace"),
        "duration_ms": int((time.time() - started) * 1000),
        "truncated": truncated,
        "timeout": False,
    })


async def exec_stream(request: web.Request) -> web.StreamResponse:
    """SSE：stdout/stderr 按行推；最后一个事件是 ``exit``。"""

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid_json"}, status=400)

    try:
        argv, timeout, _max_bytes = _build_argv(payload)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",     # nginx 反代时避免缓冲
        },
    )
    await response.prepare(request)

    _state["exec_count"] += 1
    started = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        await _sse(response, "error", {"error": f"binary_not_found: {exc}"})
        return response

    async def _pump(stream: asyncio.StreamReader, channel: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            await _sse(response, channel, text)

    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(_pump(proc.stdout, "stdout"), _pump(proc.stderr, "stderr")),
            timeout=timeout,
        )
        await proc.wait()
    except asyncio.TimeoutError:
        timed_out = True
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
    except asyncio.CancelledError:
        # 客户端断开
        proc.kill()
        raise
    except ConnectionResetError:
        proc.kill()
        return response

    await _sse(response, "exit", {
        "exit_code": proc.returncode if not timed_out else -1,
        "duration_ms": int((time.time() - started) * 1000),
        "timeout": timed_out,
    })
    return response


async def _sse(resp: web.StreamResponse, event: str, data) -> None:
    payload = (
        data if isinstance(data, str)
        else json.dumps(data, ensure_ascii=False, default=str)
    )
    msg = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
    try:
        await resp.write(msg)
    except ConnectionResetError:
        raise


# ---------- main ----------

def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw])
    app.router.add_get("/livez", livez)
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/exec", exec_oneshot)
    app.router.add_post("/v1/exec/stream", exec_stream)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    load_config()
    app = make_app()

    # SIGTERM 优雅退出（让 swarm/k8s 滚动更新时进程能干净落幕）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: loop.stop())
        except NotImplementedError:
            # Windows
            pass

    web.run_app(app, host="0.0.0.0", port=PORT, print=lambda *a, **k: None, loop=loop)


if __name__ == "__main__":
    main()
