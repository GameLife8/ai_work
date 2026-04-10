from __future__ import annotations

from docker_cli import DockerCLI


def _filter_rows(rows: list[dict], filter_name: str | None) -> list[dict]:
    if not filter_name:
        return rows
    needle = filter_name.lower()
    return [row for row in rows if needle in row.get("Name", "").lower()]


def list_services(filter_name: str | None = None) -> dict:
    cli = DockerCLI()
    rows = cli.json(["service", "ls", "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"services": _filter_rows(rows, filter_name)}


def get_service_detail(service_name: str) -> dict:
    cli = DockerCLI()
    payload = cli.json(["service", "inspect", service_name])
    return {"service": payload[0] if payload else {}}


def get_service_status(service_name: str) -> dict:
    cli = DockerCLI()
    rows = cli.json(["service", "ls", "--filter", f"name={service_name}", "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"status": rows[0] if rows else {}}


def get_service_tasks(service_name: str) -> dict:
    cli = DockerCLI()
    rows = cli.json(["service", "ps", service_name, "--no-trunc", "--format", "{{json .}}"])
    if isinstance(rows, dict):
        rows = [rows]
    return {"tasks": rows}
