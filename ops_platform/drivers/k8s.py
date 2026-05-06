from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver, DriverField
from services.k8s_client import K8sClient


class K8sDriver(ConnectionDriver):
    type_code = "k8s"
    display_name = "Kubernetes 集群"
    category = "container"

    def __init__(self) -> None:
        self.fields = [
            DriverField(
                "kubeconfig", "Kubeconfig",
                type="textarea", required=True,
                placeholder="粘贴目标集群 kubeconfig 完整 YAML",
                help="平台会落到一个临时文件，调 kubectl 时用 --kubeconfig 指向它。",
            ),
            DriverField("context", "Context",
                        help="kubeconfig 里有多 context 时显式指定；留空使用 current-context。"),
            DriverField("namespace", "默认 namespace", default="default"),
            DriverField("kubectl_bin", "kubectl 可执行", default="kubectl"),
            DriverField("log_default_tail", "默认日志行数", type="integer", default=200),
            DriverField("log_max_tail", "日志行数上限", type="integer", default=5000),
        ]

    def build_client(self, config: dict[str, Any]) -> K8sClient:
        return K8sClient(
            kubeconfig=config["kubeconfig"],
            kubectl_bin=config.get("kubectl_bin", "kubectl"),
            context=config.get("context", ""),
            namespace=config.get("namespace", "default"),
            log_default_tail=int(config.get("log_default_tail") or 200),
            log_max_tail=int(config.get("log_max_tail") or 5000),
        )
