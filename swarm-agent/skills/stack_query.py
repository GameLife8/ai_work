from __future__ import annotations

from docker_cli import DockerCLI


def list_stacks() -> dict:
    cli = DockerCLI()
    rows = cli.json(["stack", "ls", "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"stacks": rows}


def get_stack_services(stack_name: str) -> dict:
    cli = DockerCLI()
    rows = cli.json(["stack", "services", stack_name, "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"stack_name": stack_name, "services": rows}
