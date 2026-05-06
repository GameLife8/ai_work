"""HostAgent driver：把"每节点一个特权 agent 容器"接入平台。

一份 host_agent connection 对应一个集群的 agent 集合。``kind`` 决定是 K8s 还是 Swarm：
  - kind=k8s：用 kubeconfig + namespace + label_selector 找 agent pod
  - kind=swarm：用 docker_host + agent_service 找 agent 任务/容器

底层复用 K8sClient / DockerSwarmClient，再包一层 HostAgentClient，向 skill 暴露
``list_nodes`` / ``exec_on_node`` / ``nsenter_on_node`` 三个动作。
"""

from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver, DriverField
from services.docker_swarm_client import DockerSwarmClient
from services.host_agent_client import HostAgentClient, K8sHostAgent, SwarmHostAgent
from services.k8s_client import K8sClient


class HostAgentDriver(ConnectionDriver):
    type_code = "host_agent"
    display_name = "节点诊断 Agent"
    category = "container"

    def __init__(self) -> None:
        self.fields = [
            DriverField("kind", "类型", required=True, default="k8s",
                        help="k8s（DaemonSet 推荐）/ swarm（mode:global）"),

            # ---- K8s 字段 ----
            DriverField("kubeconfig", "Kubeconfig", type="textarea",
                        help="kind=k8s 时填；粘贴目标集群 kubeconfig 完整 YAML"),
            DriverField("k8s_context", "K8s Context", default=""),
            DriverField("k8s_agent_namespace", "Agent namespace", default="ai-ops",
                        help="DaemonSet 部署在哪个 namespace"),
            DriverField("k8s_agent_label", "Agent label selector", default="app=ai-ops-agent"),

            # ---- Swarm 字段 ----
            DriverField("docker_host", "DOCKER_HOST",
                        placeholder="tcp://NODE:2375",
                        help="kind=swarm 时填；建议每个节点单独注册一份 host_agent"),
            DriverField("docker_bin", "docker 可执行", default="docker"),
            DriverField("docker_tls_verify", "TLS 校验", default=""),
            DriverField("docker_cert_path", "证书路径", default=""),
            DriverField("swarm_agent_service", "Agent service 名", default="ai-ops-agent"),

            # ---- 通用 ----
            DriverField("exec_timeout_seconds", "exec 超时(秒)", type="integer", default=60),
        ]

    def build_client(self, config: dict[str, Any]) -> HostAgentClient:
        kind = (config.get("kind") or "k8s").lower()
        timeout = int(config.get("exec_timeout_seconds") or 60)

        if kind == "k8s":
            kube = K8sClient(
                kubeconfig=config["kubeconfig"],
                kubectl_bin=config.get("kubectl_bin", "kubectl"),
                context=config.get("k8s_context", ""),
                namespace=config.get("k8s_agent_namespace", "ai-ops"),
            )
            return K8sHostAgent(
                kube,
                namespace=config.get("k8s_agent_namespace", "ai-ops"),
                label_selector=config.get("k8s_agent_label", "app=ai-ops-agent"),
                exec_timeout_seconds=timeout,
            )

        if kind == "swarm":
            swarm = DockerSwarmClient(
                docker_bin=config.get("docker_bin", "docker"),
                docker_host=config["docker_host"],
                docker_tls_verify=config.get("docker_tls_verify", ""),
                docker_cert_path=config.get("docker_cert_path", ""),
            )
            return SwarmHostAgent(
                swarm,
                agent_service=config.get("swarm_agent_service", "ai-ops-agent"),
                exec_timeout_seconds=timeout,
            )

        raise ValueError(f"未知的 host_agent kind: {kind}")
