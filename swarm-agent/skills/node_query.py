from __future__ import annotations

from docker_cli import DockerCLI


def list_nodes() -> dict:
    cli = DockerCLI()
    rows = cli.json(["node", "ls", "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"nodes": rows}


def get_node_detail(node_name: str) -> dict:
    cli = DockerCLI()
    payload = cli.json(["node", "inspect", node_name])
    return {"node": payload[0] if payload else {}}


def get_cluster_info() -> dict:
    cli = DockerCLI()
    info = cli.json(["info", "--format", "{{json .}}"])
    return {"cluster": info}
