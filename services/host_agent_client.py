"""HostAgentClient：通过部署在每个节点上的特权诊断容器（"node agent"）
间接执行宿主机级别的命令，**完全替代 SSH 进宿主机排障**。

两种 transport：exec / http
-----------------------------
- ``transport=exec``（默认，老路径）：
  - K8s 用 ``kubectl exec`` 进 DaemonSet pod 跑 nsenter
  - Swarm 用 ``docker exec`` 进 mode:global 容器跑 nsenter
  - 适合 K8s 任何版本 / Docker 20.10+ swarm (能调度特权 service)
  - 优点：协议依赖少；缺点：Swarm 老版本 (18.03) 调度器拒绝特权 → exec 形同
    虚设；K8s 时受 kubectl latency 影响（~600ms / call）

- ``transport=http``（新路径，给 18.03 swarm 必备）：
  - 平台直接 HTTP POST ``http://<node-ip>:<agent_port>/v1/exec``
  - 鉴权用 connection.config.agent_token (Bearer)
  - K8s 通过 ``kubectl get node`` 拿 InternalIP；Swarm 通过 ``docker node inspect``
  - 优点：避开 kubectl/docker exec 各种限制；缺点：要求 agent 监听端口 + token 管理

按用户要求："优先 exec 然后 http"：K8s 缺省走 exec；Swarm 缺省走 http（exec 在
18.03 上根本不通）。每条 connection 可在 admin UI 覆盖。

设计要点
--------
- 我们 **不直接** 登录宿主机；agent 是一个 ``mode: global`` (Swarm) 或 DaemonSet (K8s)
  的特权容器，里头自带 ``nsenter / ss / iptables / tcpdump / ip / dmesg``。
- 复用现有 ``DockerSwarmClient`` / ``K8sClient`` 跑 docker / kubectl 命令。
- HTTP 协议跟 agent ``/v1/exec`` 对齐——请求体 ``{cmd, nsenter, timeout_sec}``。

具体 agent 协议见 docs/host-agent.md，部署 yaml 见 deploy/ 目录。
"""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass
from typing import Any

import requests

from services.docker_swarm_client import CommandResult as DockerCommandResult
from services.docker_swarm_client import DockerSwarmClient
from services.k8s_client import CommandResult as KubeCommandResult
from services.k8s_client import K8sClient


logger = logging.getLogger(__name__)


# nsenter 进入哪些 namespace 的快捷映射
NSENTER_FLAG = {
    "m": "--mount", "u": "--uts", "i": "--ipc",
    "n": "--net", "p": "--pid",  "U": "--user",
    "C": "--cgroup",
}
DEFAULT_NSENTER_NS = ("m", "u", "i", "n", "p")
DEFAULT_NSENTER_STR = "".join(DEFAULT_NSENTER_NS)        # "muinp"


def _build_nsenter_cmd(target_pid: int, namespaces: tuple[str, ...], inner: list[str]) -> list[str]:
    """构造 ``nsenter -t <pid> --mount --uts --ipc --net --pid -- <inner...>``。
    仅 exec transport 用——HTTP transport 让 agent 自己包 nsenter。"""
    cmd = ["nsenter", "-t", str(target_pid)]
    for ns in namespaces:
        if ns in NSENTER_FLAG:
            cmd.append(NSENTER_FLAG[ns])
    cmd.append("--")
    cmd.extend(inner)
    return cmd


@dataclass
class HostExecResult:
    node: str
    command: list[str]
    stdout: str
    stderr: str
    returncode: int
    transport: str = ""            # "exec" / "http" —— 给排错 / 审计用

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "command": " ".join(shlex.quote(c) for c in self.command),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "transport": self.transport,
            "ok": self.ok,
        }


# --------------------------------------------------------------------------- #
# HTTP transport —— 跟 deploy/agent 的 /v1/exec 对齐
# --------------------------------------------------------------------------- #

class _HttpExec:
    """对 agent 的 HTTP API 的薄封装。

    Agent 协议（agent/agent.py 维护）::

        POST /v1/exec
        Authorization: Bearer <token>
        { "cmd": ["ss","-ltn"], "nsenter": "muinp", "timeout_sec": 30 }
            ↓
        { "exit_code": 0, "stdout": "...", "stderr": "...",
          "duration_ms": 87, "truncated": false, "timeout": false }
    """

    def __init__(self, *, port: int, token: str, timeout_seconds: int) -> None:
        if not token:
            raise ValueError("HTTP transport 需要 agent_token")
        self.port = int(port or 9100)
        self.token = token
        self.timeout_seconds = max(5, int(timeout_seconds or 60))
        # 复用 requests.Session 维持 TCP 连接池 + 头部缓存。同一台 node 上 8 步循环
        # 里可能打 4-5 次,每次新建连接浪费 50-200ms 的 TCP/TLS 握手。
        # 线程安全说明:requests.Session 在 GET/POST 上是线程安全的（urllib3
        # 的 PoolManager 是 thread-safe）;不要在多线程间共享 cookies/adapters mutate。
        # 默认连接池 10 → 调大到 20,容纳跨节点并发查询。
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._session.headers.update({"Authorization": f"Bearer {self.token}"})

    def close(self) -> None:
        """显式释放连接池——通常 host_agent_client 跟 platform 同寿命,不需要主动调。
        给 hot-reload / 单测 cleanup 用。"""
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass

    def exec(
        self,
        node_ip: str,
        cmd: list[str],
        *,
        nsenter: str = DEFAULT_NSENTER_STR,
        container: str | None = None,
        timeout: int | None = None,
    ) -> tuple[str, str, int]:
        """发请求，返回 (stdout, stderr, returncode)。

        ``container`` 非空:让 agent **内部**解析该容器的宿主机 PID(docker inspect /
        crictl,不走业务命令白名单),并把 ``nsenter -t <pid>`` 的目标换成它——这样命令
        进的是**目标容器**的 namespace,而不是宿主机 PID 1。

        网络错误也映射成 returncode = -1 + stderr 描述，不抛——保持跟 exec 路径
        一样的"无异常返回 HostExecResult"语义。
        """
        url = f"http://{node_ip}:{self.port}/v1/exec"
        body = {
            "cmd": list(cmd),
            "nsenter": nsenter or "",
            "timeout_sec": int(timeout or self.timeout_seconds),
        }
        if container:
            body["container"] = str(container)
        try:
            # 用 self._session 复用 TCP 连接 + 已注入的 Bearer header
            resp = self._session.post(
                url,
                json=body,
                # +5s 给 agent 端 timeout 加缓冲；agent 自己也会超时切断子进程
                timeout=int(timeout or self.timeout_seconds) + 5,
            )
        except requests.RequestException as exc:
            return "", f"HTTP transport network error: {exc}", -1

        if resp.status_code != 200:
            # agent 协议：4xx/5xx 必然带 ``{"error": "..."}`` JSON 体
            try:
                payload = resp.json()
                err = payload.get("error", "")
            except (ValueError, json.JSONDecodeError):
                err = resp.text[:500]
            return "", f"HTTP transport got {resp.status_code}: {err}", -1

        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            return "", f"HTTP transport bad JSON: {exc}", -1

        # 把 agent 端的字段映射成调用方期望的格式
        return (
            data.get("stdout") or "",
            data.get("stderr") or "",
            int(data.get("exit_code", -1)),
        )

    # --- 异步任务路径（agent 1.3+） ----------------------------------------- #
    # 跟同步 /v1/exec 区别：
    #   - POST /v1/exec_async 立即 201 返回 task_id，命令在 agent 后台跑
    #   - GET  /v1/task/<id>  轮询状态（running / done / error / timeout / cancelled）
    #   - DELETE /v1/task/<id> 取消
    #   - GET  /v1/tasks      列当前节点上的所有任务
    # 为啥要这条路径：``du -sh /*`` / ``find /`` / ``tcpdump`` 这种命令同步路径
    # 走完后 HTTP 客户端早超时了，agent 又被 SIGKILL；改成异步后客户端 200ms 拿
    # task_id 走人，回头慢慢轮询。

    def _request(self, method: str, node_ip: str, path: str, *, json_body: dict | None = None,
                 timeout_seconds: int | None = None) -> tuple[int, dict]:
        """通用请求方法。失败时 status_code=-1 + ``{"error": "..."}``，不抛。"""
        url = f"http://{node_ip}:{self.port}{path}"
        try:
            # session.request 同样复用连接池 + session 已注入的 Authorization
            resp = self._session.request(
                method,
                url,
                json=json_body,
                timeout=timeout_seconds or self.timeout_seconds,
            )
        except requests.RequestException as exc:
            return -1, {"error": f"network error: {exc}"}

        try:
            data = resp.json() if resp.content else {}
        except (ValueError, json.JSONDecodeError) as exc:
            return resp.status_code, {"error": f"bad JSON: {exc}", "raw": resp.text[:500]}
        return resp.status_code, data

    def exec_async(
        self,
        node_ip: str,
        cmd: list[str],
        *,
        nsenter: str = DEFAULT_NSENTER_STR,
        max_runtime_sec: int | None = None,
        max_output_bytes: int | None = None,
    ) -> dict:
        """提交一个异步任务。返回 agent 的原始 JSON（含 task_id）或 ``{"error": "..."}``。"""
        body: dict = {
            "cmd": list(cmd),
            "nsenter": nsenter or "",
        }
        if max_runtime_sec:
            body["max_runtime_sec"] = int(max_runtime_sec)
        if max_output_bytes:
            body["max_output_bytes"] = int(max_output_bytes)
        # 提交请求本身只要 10s 内回包就行（实际任务在后台跑）
        status, data = self._request("POST", node_ip, "/v1/exec_async",
                                     json_body=body, timeout_seconds=10)
        if status != 201:
            return {"error": data.get("error") or f"submit failed: status={status}",
                    "status": status, "raw": data}
        return data

    def task_get(self, node_ip: str, task_id: str) -> dict:
        status, data = self._request("GET", node_ip, f"/v1/task/{task_id}", timeout_seconds=10)
        if status == 200:
            return data
        if status == 404:
            return {"error": "task_not_found", "task_id": task_id, "status": status}
        return {"error": data.get("error") or f"task_get failed: status={status}",
                "status": status, "raw": data}

    def task_cancel(self, node_ip: str, task_id: str) -> dict:
        status, data = self._request("DELETE", node_ip, f"/v1/task/{task_id}", timeout_seconds=10)
        return data if status in (200, 202) else {
            "error": data.get("error") or f"cancel failed: status={status}",
            "status": status, "raw": data,
        }

    def task_list(self, node_ip: str) -> dict:
        status, data = self._request("GET", node_ip, "/v1/tasks", timeout_seconds=10)
        if status == 200:
            return data
        return {"error": data.get("error") or f"task_list failed: status={status}",
                "status": status, "raw": data}


# --------------------------------------------------------------------------- #
# 抽象 + 两种实现
# --------------------------------------------------------------------------- #

class HostAgentClient:
    """统一接口；具体由 K8sHostAgent / SwarmHostAgent 实现。"""

    kind: str = ""
    transport: str = "exec"

    def list_nodes(self) -> list[str]:
        raise NotImplementedError

    def exec_on_node(self, node: str, cmd: list[str], *, timeout: int | None = None) -> HostExecResult:
        raise NotImplementedError

    # ---- 通用便捷方法 ----

    def nsenter_on_node(
        self,
        node: str,
        inner_cmd: list[str],
        *,
        target_pid: int = 1,
        namespaces: tuple[str, ...] = DEFAULT_NSENTER_NS,
        timeout: int | None = None,
    ) -> HostExecResult:
        """在指定节点的宿主机 namespace 中执行命令。

        target_pid=1 表示进入 PID=1（systemd / init）的 namespace，等同"进宿主机"。

        - exec transport：在客户端拼好 ``nsenter -t 1 --mount ... -- cmd``，整条
          发过去执行
        - http transport：直接把 ``cmd`` + ``nsenter=<flags>`` 发给 agent，由 agent
          端按本地约定包 nsenter wrap（避免客户端跟 agent 两边都拼一次）
        """
        if self.transport == "http":
            # 把 (m, u, i, n, p) tuple 拼成 "muinp" 字符串
            ns_str = "".join(namespaces) if target_pid == 1 else ""
            if target_pid != 1:
                # target_pid 非 1：HTTP 协议不支持指定 pid（agent 写死 -t 1），
                # 显式 fallback 到自己拼 nsenter 的老路径
                return self.exec_on_node(
                    node,
                    _build_nsenter_cmd(target_pid, namespaces, inner_cmd),
                    timeout=timeout,
                )
            return self._http_exec_with_nsenter(node, inner_cmd, ns_str, timeout=timeout)

        return self.exec_on_node(
            node,
            _build_nsenter_cmd(target_pid, namespaces, inner_cmd),
            timeout=timeout,
        )

    def exec_in_container_netns(
        self,
        node: str,
        container: str,
        inner_cmd: list[str],
        *,
        namespaces: tuple[str, ...] = ("n",),
        timeout: int | None = None,
    ) -> HostExecResult:
        """在**目标容器**的 namespace 里跑命令(默认只换网络 namespace)。

        跟 ``nsenter_on_node(target_pid=...)`` 的区别:**容器 PID 由 agent 内部解析**
        (docker inspect / crictl,不走业务命令白名单,也绕开 docker_proxy 对 ``docker``
        业务命令的硬拦),agent 再 ``nsenter -t <pid> <namespaces> -- cmd``。命令在 agent
        / tools 镜像的文件系统里执行(自带 dig/nslookup/ip/ss),docker_proxy 模式自动起
        ``--rm`` 特权 sibling 跑、跑完清掉。

        只 http transport 支持——exec transport(kubectl/docker exec)不走 agent 编排。
        """
        if self.transport != "http":
            raise NotImplementedError(
                "exec_in_container_netns 需要 transport=http(靠 agent 内部解析容器 PID + 编排)"
            )
        ns_str = "".join(namespaces)
        return self._http_exec_with_nsenter(
            node, inner_cmd, ns_str, container=container, timeout=timeout,
        )

    def _http_exec_with_nsenter(
        self, node: str, cmd: list[str], nsenter: str, *,
        container: str | None = None, timeout: int | None = None,
    ) -> HostExecResult:
        """子类提供 node→ip 映射后，在这里调用 _HttpExec。"""
        raise NotImplementedError("transport=http 的子类必须实现 _http_exec_with_nsenter")

    # ---- 异步任务（仅 http transport 支持；exec transport 抛 NotImplementedError）---- #

    def exec_async_on_node(
        self,
        node: str,
        cmd: list[str],
        *,
        namespaces: tuple[str, ...] = DEFAULT_NSENTER_NS,
        max_runtime_sec: int | None = None,
        max_output_bytes: int | None = None,
    ) -> dict:
        """提交异步任务（不阻塞）。返回 ``{"task_id": "...", "status": "running", ...}``。

        只在 transport=http 时可用——exec transport（kubectl/docker exec）本质是同步
        的，agent 退出 = 任务死，无法异步。
        """
        if self.transport != "http":
            raise NotImplementedError(
                f"transport={self.transport} 不支持异步任务（需要 transport=http）"
            )
        ip = self._node_ip(node)
        ns_str = "".join(namespaces)
        return self.http.exec_async(
            ip, cmd, nsenter=ns_str,
            max_runtime_sec=max_runtime_sec,
            max_output_bytes=max_output_bytes,
        )

    def task_get_on_node(self, node: str, task_id: str) -> dict:
        if self.transport != "http":
            raise NotImplementedError(
                f"transport={self.transport} 不支持异步任务查询"
            )
        return self.http.task_get(self._node_ip(node), task_id)

    def task_cancel_on_node(self, node: str, task_id: str) -> dict:
        if self.transport != "http":
            raise NotImplementedError(
                f"transport={self.transport} 不支持异步任务取消"
            )
        return self.http.task_cancel(self._node_ip(node), task_id)

    def task_list_on_node(self, node: str) -> dict:
        if self.transport != "http":
            raise NotImplementedError(
                f"transport={self.transport} 不支持异步任务列表"
            )
        return self.http.task_list(self._node_ip(node))

    def _node_ip(self, node: str) -> str:
        """子类必须实现：把 node hostname 解析成 agent 监听的 IP。"""
        raise NotImplementedError

    def healthcheck(self) -> dict:
        nodes = self.list_nodes()
        if not nodes:
            return {"healthy": False, "message": "未发现已部署的 agent 节点"}
        # 只 ping 第一个节点，避免拖时间。
        # 用 ``cat /proc/uptime`` —— ``cat`` 在 agent 白名单里（``true`` 不在），
        # 输出短而稳定，几乎所有 Linux 节点都有 /proc/uptime。
        try:
            r = self.exec_on_node(nodes[0], ["cat", "/proc/uptime"], timeout=10)
            return {
                "healthy": r.ok,
                "kind": self.kind,
                "transport": self.transport,
                "nodes": nodes,
                "probe_node": nodes[0],
                "probe_output": (r.stdout or "").strip()[:80],
                "stderr": r.stderr if not r.ok else "",
            }
        except Exception as exc:
            return {
                "healthy": False, "kind": self.kind, "transport": self.transport,
                "message": str(exc),
            }


# --------------------------------------------------------------------------- #
# K8s 实现：默认 exec（kubectl exec），可选 http
# --------------------------------------------------------------------------- #

class K8sHostAgent(HostAgentClient):
    """K8s DaemonSet 形式的 host agent。"""

    kind = "k8s"

    def __init__(
        self,
        kube: K8sClient,
        *,
        namespace: str = "ai-ops",
        label_selector: str = "app=ai-ops-agent",
        exec_timeout_seconds: int = 60,
        transport: str = "exec",          # "exec"（默认） / "http"
        http: _HttpExec | None = None,
    ) -> None:
        self.kube = kube
        self.namespace = namespace
        self.label_selector = label_selector
        self.exec_timeout_seconds = exec_timeout_seconds
        self.transport = transport if transport in {"exec", "http"} else "exec"
        if self.transport == "http" and http is None:
            raise ValueError("transport=http 时 http 参数不可为 None")
        self.http = http
        # node → internalIP 缓存（DaemonSet 节点稳定，缓存 5min 即可）
        self._ip_cache: dict[str, str] = {}

    def _list_agent_pods(self) -> list[dict]:
        result = self.kube.run([
            "-n", self.namespace, "get", "pods",
            "-l", self.label_selector,
            "-o", "json",
        ])
        if not result.ok:
            raise RuntimeError(f"kubectl get pods failed: {result.stderr}")
        data = json.loads(result.stdout or "{}")
        return data.get("items", []) or []

    def list_nodes(self) -> list[str]:
        nodes = []
        for pod in self._list_agent_pods():
            node = (pod.get("spec") or {}).get("nodeName")
            phase = (pod.get("status") or {}).get("phase")
            if node and phase == "Running":
                nodes.append(node)
        return sorted(set(nodes))

    def _find_pod_on_node(self, node: str) -> str:
        for pod in self._list_agent_pods():
            spec = pod.get("spec") or {}
            status = pod.get("status") or {}
            if spec.get("nodeName") == node and status.get("phase") == "Running":
                return (pod.get("metadata") or {}).get("name", "")
        raise RuntimeError(f"node {node} 上找不到运行中的 agent pod")

    def _node_internal_ip(self, node: str) -> str:
        if node in self._ip_cache:
            return self._ip_cache[node]
        # 也接受调用方直接传 IP（运维有时候习惯传 IP 而不是 hostname）
        if node.replace(".", "").isdigit():
            self._ip_cache[node] = node
            return node
        result = self.kube.run(["get", "node", node, "-o", "json"])
        if not result.ok:
            raise RuntimeError(f"kubectl get node {node} failed: {result.stderr}")
        data = json.loads(result.stdout or "{}")
        for addr in (data.get("status") or {}).get("addresses") or []:
            if addr.get("type") == "InternalIP":
                ip = addr["address"]
                self._ip_cache[node] = ip
                return ip
        raise RuntimeError(f"node {node} 没有 InternalIP 字段")

    def exec_on_node(self, node: str, cmd: list[str], *, timeout: int | None = None) -> HostExecResult:
        if self.transport == "http":
            ip = self._node_internal_ip(node)
            stdout, stderr, rc = self.http.exec(ip, cmd, nsenter="", timeout=timeout)
            return HostExecResult(node=node, command=cmd, stdout=stdout, stderr=stderr,
                                  returncode=rc, transport="http")
        # exec 路径
        pod = self._find_pod_on_node(node)
        full = ["-n", self.namespace, "exec", pod, "--"] + cmd
        result: KubeCommandResult = self.kube.run(full)
        return HostExecResult(node=node, command=cmd, stdout=result.stdout,
                              stderr=result.stderr, returncode=result.returncode,
                              transport="exec")

    def _http_exec_with_nsenter(
        self, node: str, cmd: list[str], nsenter: str, *,
        container: str | None = None, timeout: int | None = None,
    ) -> HostExecResult:
        ip = self._node_internal_ip(node)
        stdout, stderr, rc = self.http.exec(
            ip, cmd, nsenter=nsenter, container=container, timeout=timeout)
        return HostExecResult(node=node, command=cmd, stdout=stdout, stderr=stderr,
                              returncode=rc, transport="http")

    # 给基类异步任务路径用——所有 http 调用都走 InternalIP
    def _node_ip(self, node: str) -> str:
        return self._node_internal_ip(node)


# --------------------------------------------------------------------------- #
# Swarm 实现：默认 http（exec 在 18.03 不通），可选 exec
# --------------------------------------------------------------------------- #

class SwarmHostAgent(HostAgentClient):
    """Swarm 全局服务（mode: global）形式的 host agent。

    transport 选择
    --------------
    - **http (默认)**：跟 ``deploy/ai-ops-agent-swarm-sock-proxy.yml`` 配套。
      平台 → http://<node-ip>:9100/v1/exec，agent 自己 docker run sibling 进 host ns。
      Swarm 18.03 ~ 25.x 都能用。
    - exec (legacy)：``docker -H tcp://<node>:2375 exec``。只对**该 DOCKER_HOST 那个
      daemon 上的容器**生效，所以基本只能诊断 manager 自己；除非每个 worker 也暴露
      tcp 2375，否则跨节点 exec 不可行。Docker 20.10+ swarm 才支持调度特权 service。
    """

    kind = "swarm"

    def __init__(
        self,
        swarm: DockerSwarmClient,
        *,
        agent_service: str = "ai-ops-agent",
        exec_timeout_seconds: int = 60,
        transport: str = "http",          # "http"（默认） / "exec"
        http: _HttpExec | None = None,
    ) -> None:
        self.swarm = swarm
        self.agent_service = agent_service
        self.exec_timeout_seconds = exec_timeout_seconds
        self.transport = transport if transport in {"exec", "http"} else "http"
        if self.transport == "http" and http is None:
            raise ValueError("transport=http 时 http 参数不可为 None")
        self.http = http
        self._ip_cache: dict[str, str] = {}

    def _list_agent_tasks(self) -> list[dict]:
        rows = self.swarm.json([
            "service", "ps", self.agent_service,
            "--no-trunc", "--filter", "desired-state=running",
            "--format", "{{json .}}",
        ])
        if isinstance(rows, dict):
            rows = [rows]
        return rows or []

    def _container_id_on_node(self, node: str) -> str:
        """legacy exec 路径用——找指定 node 上的 agent 容器 ID。"""
        for row in self._list_agent_tasks():
            if row.get("Node") == node:
                task_id = row.get("ID")
                if not task_id:
                    continue
                inspect = self.swarm.json(["inspect", task_id])
                if isinstance(inspect, list) and inspect:
                    cid = (inspect[0].get("Status") or {}).get("ContainerStatus", {}).get("ContainerID")
                    if cid:
                        return cid
        raise RuntimeError(f"node {node} 上没找到运行中的 {self.agent_service} 任务/容器")

    def _node_internal_ip(self, node: str) -> str:
        if node in self._ip_cache:
            return self._ip_cache[node]
        if node.replace(".", "").isdigit():
            self._ip_cache[node] = node
            return node
        inspect = self.swarm.json(["node", "inspect", node])
        if isinstance(inspect, list) and inspect:
            ip = (inspect[0].get("Status") or {}).get("Addr") or ""
            if ip:
                self._ip_cache[node] = ip
                return ip
        raise RuntimeError(f"node {node} 在 swarm node inspect 里没拿到 Status.Addr")

    def list_nodes(self) -> list[str]:
        return sorted({row.get("Node") for row in self._list_agent_tasks() if row.get("Node")})

    def exec_on_node(self, node: str, cmd: list[str], *, timeout: int | None = None) -> HostExecResult:
        if self.transport == "http":
            ip = self._node_internal_ip(node)
            stdout, stderr, rc = self.http.exec(ip, cmd, nsenter="", timeout=timeout)
            return HostExecResult(node=node, command=cmd, stdout=stdout, stderr=stderr,
                                  returncode=rc, transport="http")
        # legacy exec 路径
        cid = self._container_id_on_node(node)
        full = ["exec", cid] + cmd
        result: DockerCommandResult = self.swarm.run(full)
        return HostExecResult(node=node, command=cmd, stdout=result.stdout,
                              stderr=result.stderr, returncode=result.returncode,
                              transport="exec")

    def _http_exec_with_nsenter(
        self, node: str, cmd: list[str], nsenter: str, *,
        container: str | None = None, timeout: int | None = None,
    ) -> HostExecResult:
        ip = self._node_internal_ip(node)
        stdout, stderr, rc = self.http.exec(
            ip, cmd, nsenter=nsenter, container=container, timeout=timeout)
        return HostExecResult(node=node, command=cmd, stdout=stdout, stderr=stderr,
                              returncode=rc, transport="http")

    def _node_ip(self, node: str) -> str:
        return self._node_internal_ip(node)
