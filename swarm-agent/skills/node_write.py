from __future__ import annotations

from docker_cli import DockerCLI


def update_node_availability(node_name: str, availability: str) -> dict:
    cli = DockerCLI()
    result = cli.run(["node", "update", "--availability", availability, node_name])
    if not result.ok:
        raise RuntimeError(result.stderr or "更新节点可用性失败")
    return {"message": "节点可用性更新命令已提交", "output": result.stdout}
