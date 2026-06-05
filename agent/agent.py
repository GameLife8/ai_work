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
import secrets
import signal
import socket
import time
from pathlib import Path

import yaml
from aiohttp import web


AGENT_VERSION = "1.3.0"

# ---------- 配置 ----------

PORT = int(os.getenv("AGENT_PORT", "9100"))
TOKEN_FILE = os.getenv("AGENT_AUTH_TOKEN_FILE", "/run/secrets/token")
TOKEN_ENV = os.getenv("AGENT_AUTH_TOKEN", "")          # 仅 dev/测试用；生产请用 file
ALLOWED_CIDR = os.getenv("AGENT_ALLOWED_CIDR", "")     # 逗号分隔；空 = 不做 IP 限制
ALLOW_FILE = os.getenv("AGENT_ALLOWED_FILE", "/etc/ai-ops-agent/allowed.yml")
NODE_NAME = os.getenv("AGENT_NODE_NAME") or socket.gethostname()

# Agent 执行模式
#   direct       —— Agent 直接在自己进程里跑 nsenter -t 1。要求容器自己有
#                    privileged + pid:host + network:host（K8s DaemonSet 默认走这条）。
#   docker_proxy —— Agent 不要任何特权，但挂 /var/run/docker.sock；收到 /v1/exec 时
#                    通过本地 docker daemon ``docker run --rm --privileged --pid host``
#                    起一次性 sibling 容器跑 nsenter。
#                    专为 Docker 18.03 / 老版 swarm 用——它们的 service 调度器拒绝
#                    privileged 类字段，但 ``docker run`` 始终支持。
AGENT_MODE = os.getenv("AGENT_MODE", "direct").strip().lower()
if AGENT_MODE not in {"direct", "docker_proxy"}:
    raise RuntimeError(f"AGENT_MODE 必须是 direct 或 docker_proxy，当前: {AGENT_MODE!r}")

# docker_proxy 模式下用哪个镜像作为 sibling 跑 nsenter。默认沿用 agent 自己的镜像
# （它里面已经有 nsenter / iproute2 / iptables / tcpdump），省一次镜像维护。
AGENT_TOOLS_IMAGE = os.getenv("AGENT_TOOLS_IMAGE", "")

# docker_proxy 模式下调本地 docker daemon 的 CLI；alpine 包名 docker-cli
DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")

# 全局上限——单次调用兜底
MAX_TIMEOUT_SEC = 300         # /v1/exec 同步路径硬上限
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_OUTPUT_BYTES = 1024 * 1024

# /v1/exec_async 异步路径上限：远比同步路径宽，给 ``du -sh /*`` /
# ``find / -size`` 这种长任务用
ASYNC_MAX_RUNTIME_SEC = 30 * 60      # 30 分钟，agent 内强制 SIGKILL
ASYNC_DEFAULT_RUNTIME_SEC = 5 * 60
ASYNC_MAX_CONCURRENT_TASKS = 16      # 同时跑的异步任务上限，超就 429
ASYNC_RETAIN_DONE_SEC = 30 * 60      # 完成后保留结果可查的时长

# 运行时状态（不需要原子操作；GIL 已经保证 int +1 原子）
_state: dict[str, object] = {
    "token": "",
    "allowed_commands": set(),
    "start_time": time.time(),
    "exec_count": 0,
    "tasks": {},                # task_id -> TaskState dict（异步任务）
}

logger = logging.getLogger("ai-ops-agent")


# ---------- 启动配置加载 ----------

def load_config() -> None:
    """读 token + 白名单 + 模式校验。任一缺失都直接退出——agent 不能裸跑。"""

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

    # docker_proxy 模式必须能调到 docker CLI + 必须设 tools 镜像
    if AGENT_MODE == "docker_proxy":
        if not AGENT_TOOLS_IMAGE:
            raise RuntimeError(
                "AGENT_MODE=docker_proxy 需要设 AGENT_TOOLS_IMAGE 环境变量"
                "（指定一个内含 nsenter + iproute2 + iptables 的镜像，"
                "通常直接复用 agent 自己的镜像即可）"
            )
        # 兜一下：docker.sock 必须挂上
        if not Path("/var/run/docker.sock").exists():
            raise RuntimeError(
                "AGENT_MODE=docker_proxy 但 /var/run/docker.sock 不存在——"
                "确认部署 YAML 里挂了 host docker socket"
            )

    logger.info(
        "agent 启动配置就绪：node=%s port=%s mode=%s allowed=%s",
        NODE_NAME, PORT, AGENT_MODE, sorted(cmds),
    )
    if AGENT_MODE == "docker_proxy":
        logger.info("docker_proxy 模式 tools image = %s", AGENT_TOOLS_IMAGE)


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
    """从请求体造最终 argv + timeout + max_output_bytes。

    direct 模式：``[nsenter -t 1 ... -- <cmd>]`` —— 在 agent 进程里直接 exec。
    docker_proxy 模式：``[docker run --rm --privileged --pid host --network host
                          --mount /:/ host:ro --entrypoint nsenter <tools-image>
                          -t 1 ... -- <cmd>]`` —— 起 sibling 容器跑 nsenter。

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
    # docker_proxy 模式下也禁止用户直接调 docker（避免穿透到本地 daemon）
    if AGENT_MODE == "docker_proxy" and base == "docker":
        raise PermissionError(
            "docker_proxy 模式下不能直接调 docker；agent 自己会用 docker run 起 sibling"
        )

    # nsenter 配置：默认进 m/u/i/n/p；显式传 "" 表示**不**进 host ns
    ns_str = payload.get("nsenter")
    if ns_str is None:
        ns_str = "muinp"
    if not isinstance(ns_str, str):
        raise ValueError("nsenter 必须是字符串，如 'muinp' 或 ''")

    # nsenter 目标 PID：默认 1（宿主机 init = "进宿主机"）。进**容器** namespace 时,
    # 由 exec handler 先把 ``container`` 解析成宿主机 PID 塞进 ``payload["target_pid"]``。
    # 容器排障一般只传 ``nsenter="n"``（只换网络 namespace）→ 命令仍在 agent/tools 镜像的
    # mount namespace 里找,dig/nslookup/ip/ss 这些工具都在,不会 executable not found。
    target_pid = payload.get("target_pid")
    try:
        target_pid = int(target_pid) if target_pid is not None else 1
    except (TypeError, ValueError):
        raise ValueError("target_pid 必须是正整数")
    if target_pid <= 0:
        raise ValueError("target_pid 必须 > 0")

    # 拼 nsenter 段
    if ns_str:
        ns_args: list[str] = ["-t", str(target_pid)]
        for ch in ns_str:
            flag = _NS_FLAGS.get(ch)
            if not flag:
                raise ValueError(f"nsenter 字符 {ch!r} 未识别")
            ns_args.append(flag)
        ns_args.append("--")

    if AGENT_MODE == "direct":
        # 老路径：agent 自己 nsenter
        if ns_str:
            argv = ["nsenter", *ns_args, *cmd]
        else:
            argv = list(cmd)
    else:
        # docker_proxy 路径：把整个执行包到 sibling 容器里
        #
        # 关键挂载（必须，对称于 K8s direct 模式 agent 容器本身就挂着的那批）：
        #   1. /:/host (ro) —— sibling 内 ``cat /host/etc/...`` 等
        #   2. /var/run/docker.sock —— sibling 内的 ``docker ps`` / ``docker rm``
        #      默认走 unix socket；它自己的 mount ns 是空的，不挂就报 "Cannot connect
        #      to the Docker daemon at unix:///var/run/docker.sock"。
        #      ns_str=="" (admin maintenance 扫 sibling 容器走这条) 时尤其关键——
        #      没 nsenter 切到 host mount ns，全靠这个 bind mount 才能让 docker CLI
        #      看到 socket。带 nsenter 时这个 bind 会被 mount-ns 切换"盖掉"，但
        #      host 自己的 /var/run/docker.sock 就在同一路径，所以不冲突。
        docker_argv = [
            DOCKER_BIN, "run", "--rm",
            "--privileged",
            "--pid", "host",
            "--network", "host",
            # 宿主机文件系统挂到 sibling 内 /host，方便 cat /host/etc/...
            "--mount", "type=bind,src=/,dst=/host,readonly,bind-propagation=rshared",
            # 宿主机 docker socket —— 让 sibling 里的 docker CLI 能连上 host daemon
            "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
        ]
        if ns_str:
            # entrypoint = nsenter，把 nsenter flags + 业务 cmd 当 ARGS 喂进去
            docker_argv += ["--entrypoint", "nsenter", AGENT_TOOLS_IMAGE]
            docker_argv += ns_args + cmd
        else:
            # 不进 host ns 时：sibling 里直接跑业务命令
            docker_argv += ["--entrypoint", cmd[0], AGENT_TOOLS_IMAGE]
            docker_argv += cmd[1:]
        argv = docker_argv

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


# ---------- 容器 PID 解析（agent 内部编排，不走业务白名单） ----------
#
# 为什么放 agent 内部而不是让 skill 发 ``docker inspect`` 业务命令：
#   1. ``docker`` 不在 allowed.yml 白名单 → 业务命令会 403；
#   2. docker_proxy 模式还**额外硬拦** ``docker`` 业务命令（防穿透本地 daemon）。
# 但 agent 进程本来就挂着 ``/var/run/docker.sock`` + 自带 docker-cli（direct 模式 k8s
# manifest 也挂了 docker.sock / containerd.sock），所以 agent **自己**跑 docker inspect /
# crictl 拿 PID 是合法编排，绕开上面两条限制。

def _safe_int(s) -> int | None:
    try:
        return int(str(s or "").strip())
    except (TypeError, ValueError):
        return None


async def _run_capture(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """跑一条 agent **内部**命令（不经 /v1/exec 白名单），返回 (rc, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return 127, "", f"{argv[0]}: not found ({exc})"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"{argv[0]}: timeout"
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _resolve_container_pid(container: str) -> tuple[int | None, str, str]:
    """容器名/ID → 宿主机主进程 PID（docker → crictl → ctr）。返回 (pid, runtime, err)。"""
    # 1. docker（Swarm / docker 节点）—— 先精确 inspect
    rc, out, err1 = await _run_capture(
        ["docker", "inspect", "--format", "{{.State.Pid}}", container])
    pid = _safe_int(out)
    if rc == 0 and pid:
        return pid, "docker", ""
    # docker inspect 没命中 → 按名字**前缀/子串**找该节点上的容器(Swarm task 名是
    # ``svc.slot.taskid``,用户/模型往往只给 service 名 ``svc``)。取第一个匹配。
    rc_ps, out_ps, _ = await _run_capture(
        ["docker", "ps", "-q", "--filter", f"name={container}"])
    cid = out_ps.strip().splitlines()[0] if (rc_ps == 0 and out_ps.strip()) else ""
    if cid:
        rc2, out2, e2 = await _run_capture(
            ["docker", "inspect", "--format", "{{.State.Pid}}", cid])
        pid = _safe_int(out2)
        if rc2 == 0 and pid:
            return pid, "docker", ""
        err1 = (e2 or err1)
    # 2. crictl（K8s containerd / CRI-O）。传进来的可能是**容器名**,也可能是 **Pod 名**:
    #    a) 先按容器名找(``crictl ps --name`` 匹配的是容器名)
    #    b) 没中 → 按 Pod 名找(``crictl pods --name`` → pod id → 该 pod 第一个容器)
    #    c) 还没中 → 当成原始容器 id 前缀直接 inspect
    rc, out, _ = await _run_capture(["crictl", "ps", "-q", "--name", container])
    cid = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else ""
    if not cid:
        rc, out, _ = await _run_capture(["crictl", "pods", "-q", "--name", container])
        pod_id = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else ""
        if pod_id:
            rc, out, _ = await _run_capture(["crictl", "ps", "-q", "--pod", pod_id])
            cid = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else ""
    if not cid:
        cid = container
    rc, out, err2 = await _run_capture(
        ["crictl", "inspect", "-o", "go-template", "--template", "{{.info.pid}}", cid])
    pid = _safe_int(out)
    if rc == 0 and pid:
        return pid, "crictl", ""
    # 3. ctr 兜底
    rc, out, err3 = await _run_capture(
        ["ctr", "-n", "k8s.io", "container", "info", container])
    if rc == 0 and out.strip().startswith("{"):
        try:
            cpid = (json.loads(out).get("Status") or {}).get("Pid")
            if isinstance(cpid, int) and cpid > 0:
                return cpid, "ctr", ""
        except json.JSONDecodeError:
            pass
    return None, "", (
        f"docker:[{err1.strip()}] crictl:[{err2.strip()}] ctr:[{err3.strip()}]"
    )


async def _apply_container_target(payload: dict):
    """若请求带 ``container``，解析成 PID 塞进 ``payload['target_pid']``。

    返回 ``None`` 表示 OK（或没传 container）；返回 ``web.Response`` 表示解析失败，
    调用方应直接把它返回给客户端。
    """
    container = payload.get("container")
    if not container:
        return None
    pid, runtime, err = await _resolve_container_pid(str(container))
    if not pid:
        return web.json_response(
            {"error": f"container_pid_not_found: {err}", "container": container},
            status=404,
        )
    payload["target_pid"] = pid
    payload["_container_runtime"] = runtime
    return None


# ---------- 路由 handlers ----------

async def livez(_request: web.Request) -> web.Response:
    """无鉴权探活——给 healthcheck 用，不带任何敏感信息。"""
    return web.Response(text="ok", content_type="text/plain")


async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "node": NODE_NAME,
        "agent_version": AGENT_VERSION,
        "agent_mode": AGENT_MODE,
        "tools_image": AGENT_TOOLS_IMAGE if AGENT_MODE == "docker_proxy" else None,
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

    # 容器 netns 排障:把 ``container`` 解析成宿主机 PID 塞进 payload（agent 内部编排）
    err_resp = await _apply_container_target(payload)
    if err_resp is not None:
        return err_resp

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

    # 容器 netns 排障:把 ``container`` 解析成宿主机 PID 塞进 payload（agent 内部编排）
    err_resp = await _apply_container_target(payload)
    if err_resp is not None:
        return err_resp

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


# ============================================================================
#  Async task path —— /v1/exec_async / /v1/task/<id>
# ============================================================================
#
# 痛点：``du -sh /*`` 这种命令在大盘上跑 5+ 分钟很正常，远超 ``/v1/exec``
# 60s 同步上限。直接超时会让 LLM 拿到"已超时无输出"残废结果，写出来的诊断报告毫无价值。
#
# 解法：另开一条 ``/v1/exec_async`` 路径——立即返回 task_id，命令在 agent 的
# asyncio loop 里跑（独立 task），结果累积到内存 ``_state["tasks"]`` 表。
# 调用方（平台 skill / LLM）轮询 ``GET /v1/task/<id>`` 拿当前状态。
#
# 设计取舍：
#   - 任务**只活在 agent 进程内存里**，agent 容器重启 = 任务消失。这是 acceptable，
#     因为 swarm/k8s 会自动重启，且任务最长 30min，跨越运维周期窗口短。
#   - 任务 ASYNC_MAX_CONCURRENT_TASKS=16 上限——防内存爆炸。
#   - 完成后保留 ASYNC_RETAIN_DONE_SEC=30min 让调用方有机会取结果，之后 GC 掉。
#   - SIGTERM → 2s → SIGKILL 跟同步路径一致。


def _new_task_id() -> str:
    return secrets.token_urlsafe(16)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _gc_done_tasks() -> int:
    """清掉完成超过 ASYNC_RETAIN_DONE_SEC 的任务。返回清掉数。"""
    tasks: dict = _state["tasks"]
    now = time.time()
    purged = 0
    for tid in list(tasks.keys()):
        t = tasks[tid]
        if t.get("status") in {"running"}:
            continue
        ended = t.get("_ended_epoch", 0)
        if ended and (now - ended) > ASYNC_RETAIN_DONE_SEC:
            del tasks[tid]
            purged += 1
    return purged


def _running_task_count() -> int:
    return sum(1 for t in _state["tasks"].values() if t.get("status") == "running")


def _public_task_view(t: dict) -> dict:
    """把内部 TaskState 中"私有字段"剥掉，给 HTTP 客户端用。"""
    excl = {"_proc", "_runner_task", "_ended_epoch", "_started_epoch", "_max_output_bytes"}
    return {k: v for k, v in t.items() if k not in excl}


async def exec_async(request: web.Request) -> web.Response:
    """提交一个异步任务，立即返回 task_id。"""
    _gc_done_tasks()
    if _running_task_count() >= ASYNC_MAX_CONCURRENT_TASKS:
        return web.json_response(
            {"error": "too_many_running_tasks",
             "running": _running_task_count(),
             "limit": ASYNC_MAX_CONCURRENT_TASKS},
            status=429,
        )

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid_json"}, status=400)

    try:
        argv, _ignored_sync_timeout, max_bytes = _build_argv(payload)
    except PermissionError as exc:
        return web.json_response({"error": str(exc)}, status=403)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    # 异步路径用独立的 max_runtime_sec 字段（单位秒），跟同步 timeout_sec 区分
    max_runtime = float(payload.get("max_runtime_sec") or ASYNC_DEFAULT_RUNTIME_SEC)
    max_runtime = min(max(max_runtime, 5), ASYNC_MAX_RUNTIME_SEC)

    # 调用方可在 body 里传 ``task_id``——平台 DB 是 source of truth，需要在调 agent
    # **之前**就生成 PK 并写库，再把同一个 ID 透传给 agent。如果调用方不传就 fallback
    # 到本地生成。冲突保护：传进来的 task_id 必须没被占用。
    client_task_id = payload.get("task_id")
    if client_task_id:
        if not isinstance(client_task_id, str) or not (4 <= len(client_task_id) <= 64):
            return web.json_response(
                {"error": "task_id must be a 4~64 char string"}, status=400,
            )
        if client_task_id in _state["tasks"]:
            return web.json_response(
                {"error": "task_id_collision", "task_id": client_task_id}, status=409,
            )
        task_id = client_task_id
    else:
        task_id = _new_task_id()
    now = time.time()
    task: dict = {
        "task_id":     task_id,
        "status":      "running",        # running / done / timeout / error / cancelled
        "stdout":      "",
        "stderr":      "",
        "exit_code":   None,
        "started_at":  _now_iso(),
        "ended_at":    None,
        "duration_ms": None,
        "max_runtime_sec": max_runtime,
        "command":     argv,
        "nsenter":     payload.get("nsenter", "muinp"),
        "truncated":   False,
        "_started_epoch": now,
        "_max_output_bytes": max_bytes,
    }
    _state["tasks"][task_id] = task

    # 在 asyncio loop 里 fire-and-forget 跑 _run_task；不 await
    runner = asyncio.create_task(_run_task(task_id, argv, max_runtime, max_bytes))
    task["_runner_task"] = runner

    _state["exec_count"] += 1
    logger.info("async task %s started: %s", task_id, " ".join(argv[:8]))

    return web.json_response({
        "task_id":   task_id,
        "status":    "running",
        "started_at": task["started_at"],
        "max_runtime_sec": max_runtime,
        "poll_endpoint": f"/v1/task/{task_id}",
    }, status=201)


async def _run_task(task_id: str, argv: list[str], max_runtime: float, max_bytes: int) -> None:
    """真正跑子进程的协程；存活在 agent loop 内，结果回写 _state['tasks'][task_id]。"""
    task = _state["tasks"].get(task_id)
    if task is None:
        return

    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        task["_proc"] = proc
    except FileNotFoundError as exc:
        task.update({
            "status": "error",
            "stderr": f"binary_not_found: {exc}",
            "exit_code": -1,
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "_ended_epoch": time.time(),
        })
        return

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max_runtime)
    except asyncio.TimeoutError:
        timed_out = True
        proc.terminate()
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()
            stdout = stderr = b""
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
    except asyncio.CancelledError:
        # 客户端调了 DELETE /v1/task/<id>
        proc.kill()
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        task.update({
            "status": "cancelled",
            "exit_code": -1,
            "stderr": "cancelled by client",
            "ended_at": _now_iso(),
            "duration_ms": int((time.time() - started) * 1000),
            "_ended_epoch": time.time(),
        })
        return

    sb = stdout or b""
    se = stderr or b""
    truncated = False
    if len(sb) > max_bytes:
        sb = sb[:max_bytes]
        truncated = True
    if len(se) > max_bytes:
        se = se[:max_bytes]
        truncated = True

    if timed_out:
        status = "timeout"
        exit_code = -1
        se = se + (f"\n[agent] task exceeded max_runtime_sec={max_runtime}".encode())
    elif proc.returncode == 0:
        status = "done"
        exit_code = 0
    else:
        status = "error"
        exit_code = proc.returncode if proc.returncode is not None else -1

    task.update({
        "status": status,
        "stdout": sb.decode("utf-8", errors="replace"),
        "stderr": se.decode("utf-8", errors="replace"),
        "exit_code": exit_code,
        "ended_at": _now_iso(),
        "duration_ms": int((time.time() - started) * 1000),
        "truncated": truncated,
        "_ended_epoch": time.time(),
    })
    logger.info(
        "async task %s finished: status=%s exit=%s duration_ms=%s",
        task_id, status, exit_code, task["duration_ms"],
    )


async def task_get(request: web.Request) -> web.Response:
    task_id = request.match_info.get("task_id", "")
    _gc_done_tasks()
    task = _state["tasks"].get(task_id)
    if task is None:
        return web.json_response({"error": "task_not_found", "task_id": task_id}, status=404)
    return web.json_response(_public_task_view(task))


async def task_cancel(request: web.Request) -> web.Response:
    task_id = request.match_info.get("task_id", "")
    task = _state["tasks"].get(task_id)
    if task is None:
        return web.json_response({"error": "task_not_found", "task_id": task_id}, status=404)
    if task["status"] != "running":
        # 已完成；幂等返回当前状态
        return web.json_response(_public_task_view(task))
    runner = task.get("_runner_task")
    if isinstance(runner, asyncio.Task):
        runner.cancel()
    return web.json_response({"task_id": task_id, "status": "cancelling"})


async def task_list(_request: web.Request) -> web.Response:
    """列当前内存里所有任务（含 done/running/error）。给运维排查 + LLM 主动查询用。"""
    _gc_done_tasks()
    items = sorted(
        _state["tasks"].values(),
        key=lambda t: t.get("_started_epoch", 0),
        reverse=True,
    )
    return web.json_response({
        "tasks": [_public_task_view(t) for t in items],
        "running": _running_task_count(),
        "limits": {
            "max_concurrent": ASYNC_MAX_CONCURRENT_TASKS,
            "max_runtime_sec": ASYNC_MAX_RUNTIME_SEC,
            "retain_done_sec": ASYNC_RETAIN_DONE_SEC,
        },
    })


# ---------- main ----------

def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw])
    app.router.add_get("/livez", livez)
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/exec", exec_oneshot)
    app.router.add_post("/v1/exec/stream", exec_stream)
    # 异步任务
    app.router.add_post("/v1/exec_async", exec_async)
    app.router.add_get("/v1/task/{task_id}", task_get)
    app.router.add_delete("/v1/task/{task_id}", task_cancel)
    app.router.add_get("/v1/tasks", task_list)
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
