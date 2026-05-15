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

    def exec(
        self,
        node_ip: str,
        cmd: list[str],
        *,
        nsenter: str = DEFAULT_NSENTER_STR,
        timeout: int | None = None,
    ) -> tuple[str, str, int]:
        """发请求，返回 (stdout, stderr, returncode)。

        网络错误也映射成 returncode = -1 + stderr 描述，不抛——保持跟 exec 路径
        一样的"无异常返回 HostExecResult"语义。
        """
        url = f"http://{node_ip}:{self.port}/v1/exec"
        body = {
            "cmd": list(cmd),
            "nsenter": nsenter or "",
            "timeout_sec": int(timeout or self.timeout_seconds),
        }
        try:
            resp = requests.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
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

    def _http_exec_with_nsenter(
        self, node: str, cmd: list[str], nsenter: str, *, timeout: int | None = None,
    ) -> HostExecResult:
        """子类提供 node→ip 映射后，在这里调用 _HttpExec。"""
        raise NotImplementedError("transport=http 的子类必须实现 _http_exec_with_nsenter")

    def healthcheck(self) -> dict:
        nodes = self.list_nodes()
        if not nodes:
            return {"healthy": False, "message": "未发现已部署的 agent 节点"}
        # 只 ping 第一个节点，避免拖时间
        try:
            r = self.exec_on_node(nodes[0], ["true"], timeout=10)
            return {
                "healthy": r.ok,
                "kind": self.kind,
                "transport": self.transport,
                "nodes": nodes,
                "probe_node": nodes[0],
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
        self, node: str, cmd: list[str], nsenter: str, *, timeout: int | None = None,
    ) -> HostExecResult:
        ip = self._node_internal_ip(node)
        stdout, stderr, rc = self.http.exec(ip, cmd, nsenter=nsenter, timeout=timeout)
        return HostExecResult(node=node, command=cmd, stdout=stdout, stderr=stderr,
                              returncode=rc, transport="http")


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
        self, node: str, cmd: list[str], nsenter: str, *, timeout: int | None = None,
    ) -> HostExecResult:
        ip = self._node_internal_ip(node)
        stdout, stderr, rc = self.http.exec(ip, cmd, nsenter=nsenter, timeout=timeout)
        return HostExecResult(node=node, command=cmd, stdout=stdout, stderr=stderr,
                              returncode=rc, transport="http")
