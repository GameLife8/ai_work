from __future__ import annotations

from config import get_settings
from docker_cli import DockerCLI


def get_service_logs(service_name: str, tail: int | None = None, since: str | None = None) -> dict:
    cli = DockerCLI()
    settings = get_settings()
    final_tail = min(tail or settings.log_default_tail, settings.log_max_tail)
    args = ["service", "logs", service_name, "--tail", str(final_tail), "--raw"]
    if since:
        args.extend(["--since", since])
    result = cli.run(args)
    if not result.ok:
        raise RuntimeError(result.stderr or "获取服务日志失败")
    return {
        "service_name": service_name,
        "tail": final_tail,
        "since": since,
        "logs": result.stdout,
    }
