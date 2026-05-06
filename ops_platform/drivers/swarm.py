from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver, DriverField
from services.docker_swarm_client import DockerSwarmClient


class SwarmDriver(ConnectionDriver):
    type_code = "swarm"
    display_name = "Docker Swarm 集群"
    category = "container"

    def __init__(self) -> None:
        self.fields = [
            DriverField("docker_host", "DOCKER_HOST", required=True,
                        placeholder="tcp://1.2.3.4:2375",
                        help="远端 Swarm manager 节点的 docker daemon 地址"),
            DriverField("docker_bin", "docker 可执行", default="docker"),
            DriverField("docker_tls_verify", "TLS 校验", default="",
                        help="启用 mTLS 时设为 1，并配证书路径（容器内挂载）"),
            DriverField("docker_cert_path", "证书路径", default=""),
            DriverField("log_default_tail", "默认日志行数", type="integer", default=100),
            DriverField("log_max_tail", "日志行数上限", type="integer", default=1000),
        ]

    def build_client(self, config: dict[str, Any]) -> DockerSwarmClient:
        return DockerSwarmClient(
            docker_bin=config.get("docker_bin", "docker"),
            docker_host=config["docker_host"],
            docker_tls_verify=config.get("docker_tls_verify", ""),
            docker_cert_path=config.get("docker_cert_path", ""),
            log_default_tail=int(config.get("log_default_tail") or 100),
            log_max_tail=int(config.get("log_max_tail") or 1000),
        )
