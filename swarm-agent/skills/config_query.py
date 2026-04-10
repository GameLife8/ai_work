from __future__ import annotations

from docker_cli import DockerCLI


def list_configs() -> dict:
    cli = DockerCLI()
    rows = cli.json(["config", "ls", "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"configs": rows}


def get_config_detail(config_name: str) -> dict:
    cli = DockerCLI()
    payload = cli.json(["config", "inspect", config_name])
    return {"config": payload[0] if payload else {}}
