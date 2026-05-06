"""HostAgentClient：通过部署在每个节点上的特权诊断容器（"node agent"）
间接执行宿主机级别的命令，**完全替代 SSH 进宿主机排障**。

设计要点
--------
- 我们 **不直接** 登录宿主机；agent 是一个 ``mode: global`` (Swarm) 或 DaemonSet (K8s)
  的特权容器，里头自带 ``nsenter / ss / iptables / tcpdump / ip / dmesg``。
- 平台调用流程：
    skill -> HostAgentClient.exec_on_node(node, cmd)
        -> kubectl/docker exec <agent_pod_or_container> <cmd>
        -> 一般 cmd 是 ``nsenter -t 1 -m -u -i -n -p <真实命令>``，
           这样命令在宿主机的 namespace 下执行，看到的是宿主机自己的 socket /
           iptables / 进程 / dmesg。
- 复用现有 ``DockerSwarmClient`` / ``K8sClient`` 跑 docker / kubectl 命令，不再重复造轮子。

部署侧约定
----------
agent 容器满足：
  * Swarm: ``mode: global``，``pid=host``，``network=host``，``privileged=true``，
           挂 ``/:/host:ro,rshared``；service 名约定为 ``ai-ops-agent``（admin 可改）。
  * K8s: DaemonSet，``hostPID=hostNetwork=hostIPC=true``，securityContext.privileged=true，
         label ``app=ai-ops-agent``（admin 可改）。

具体 yaml 见 ``deploy/`` 目录。
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from services.docker_swarm_client import CommandResult as DockerCommandResult
from services.docker_swarm_client import DockerSwarmClient
from services.k8s_client import CommandResult as KubeCommandResult
from services.k8s_client import K8sClient


# nsenter 进入哪些 namespace 的快捷映射
NSENTER_FLAG = {
    "m": "--mount", "u": "--uts", "i": "--ipc",
    "n": "--net", "p": "--pid",  "U": "--user",
    "C": "--cgroup",
}
DEFAULT_NSENTER_NS = ("m", "u", "i", "n", "p")


def _build_nsenter_cmd(target_pid: int, namespaces: tuple[str, ...], inner: list[str]) -> list[str]:
    """构造 ``nsenter -t <pid> --mount --uts --ipc --net --pid -- <inner...>``。"""
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
            "ok": self.ok,
        }


# --------------------------------------------------------------------------- #
# 抽象 + 两种实现
# --------------------------------------------------------------------------- #

class HostAgentClient:
    """统一接口；具体由 K8sHostAgent / SwarmHostAgent 实现。"""

    kind: str = ""

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
        想进某个容器的网络 namespace，把 target_pid 设成那个容器的宿主机 PID。
        """
        return self.exec_on_node(
            node,
            _build_nsenter_cmd(target_pid, namespaces, inner_cmd),
            timeout=timeout,
        )

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
                "nodes": nodes,
                "probe_node": nodes[0],
                "stderr": r.stderr if not r.ok else "",
            }
        except Exception as exc:
            return {"healthy": False, "kind": self.kind, "message": str(exc)}


# --------------------------------------------------------------------------- #
# K8s 实现：成熟、稳定推荐使用
# --------------------------------------------------------------------------- #

class K8sHostAgent(HostAgentClient):
    """K8s DaemonSet 形式的 host agent。

    通过 kubectl 在指定 namespace + label 下找到目标节点的 pod，再 ``kubectl exec`` 进去。
    """

    kind = "k8s"

    def __init__(
        self,
        kube: K8sClient,
        *,
        namespace: str = "ai-ops",
        label_selector: str = "app=ai-ops-agent",
        exec_timeout_seconds: int = 60,
    ) -> None:
        self.kube = kube
        self.namespace = namespace
        self.label_selector = label_selector
        self.exec_timeout_seconds = exec_timeout_seconds

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

    def exec_on_node(self, node: str, cmd: list[str], *, timeout: int | None = None) -> HostExecResult:
        pod = self._find_pod_on_node(node)
        full = ["-n", self.namespace, "exec", pod, "--"] + cmd
        result: KubeCommandResult = self.kube.run(full)
        return HostExecResult(
            node=node,
            command=cmd,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )


# --------------------------------------------------------------------------- #
# Swarm 实现：用 docker CLI 经由 manager 找到 agent 任务再 exec
# --------------------------------------------------------------------------- #

class SwarmHostAgent(HostAgentClient):
    """Swarm 全局服务（mode: global）形式的 host agent。

    限制说明
    --------
    Docker Swarm 下 ``docker exec`` 只对 **本地 daemon 上的容器** 生效。
    所以 ``docker_host`` 必须配成"对所有节点都可达的入口"，常见做法：
      * 把每个 node 的 docker daemon 暴露在 tcp://NODE:2375（内网，需配 mTLS）
      * 然后这里的 ``docker_host`` 配置成 manager；同时给每个 node 单独配
        ``host_agent`` connection 即可
    简单起见，本类直接用 ``docker_host`` 当作"能访问当前节点"的入口；
    若你希望跨节点 exec，给每个 node 注册一份 host_agent connection。
    """

    kind = "swarm"

    def __init__(
        self,
        swarm: DockerSwarmClient,
        *,
        agent_service: str = "ai-ops-agent",
        exec_timeout_seconds: int = 60,
    ) -> None:
        self.swarm = swarm
        self.agent_service = agent_service
        self.exec_timeout_seconds = exec_timeout_seconds

    def _list_agent_tasks(self) -> list[dict]:
        """查 service 的所有 task，返回包含 Node / ContainerID 的精简结构。"""
        rows = self.swarm.json([
            "service", "ps", self.agent_service,
            "--no-trunc", "--filter", "desired-state=running",
            "--format", "{{json .}}",
        ])
        if isinstance(rows, dict):
            rows = [rows]
        # 拿 Node 名；ContainerID 需要再 inspect 任务
        return rows or []

    def _container_id_on_node(self, node: str) -> str:
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

    def list_nodes(self) -> list[str]:
        return sorted({row.get("Node") for row in self._list_agent_tasks() if row.get("Node")})

    def exec_on_node(self, node: str, cmd: list[str], *, timeout: int | None = None) -> HostExecResult:
        cid = self._container_id_on_node(node)
        full = ["exec", cid] + cmd
        result: DockerCommandResult = self.swarm.run(full)
        return HostExecResult(
            node=node,
            command=cmd,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )
