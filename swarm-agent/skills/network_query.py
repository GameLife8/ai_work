from __future__ import annotations

from docker_cli import DockerCLI


def list_networks() -> dict:
    cli = DockerCLI()
    rows = cli.json(["network", "ls", "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"networks": rows}


def get_network_detail(network_name: str) -> dict:
    cli = DockerCLI()
    payload = cli.json(["network", "inspect", network_name])
    return {"network": payload[0] if payload else {}}
