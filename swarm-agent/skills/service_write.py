from __future__ import annotations

from docker_cli import DockerCLI


def update_service_image(service_name: str, new_image: str) -> dict:
    cli = DockerCLI()
    result = cli.run(["service", "update", "--image", new_image, service_name])
    if not result.ok:
        raise RuntimeError(result.stderr or "更新服务镜像失败")
    return {"message": "服务镜像更新命令已提交", "output": result.stdout}


def scale_service(service_name: str, replicas: int) -> dict:
    cli = DockerCLI()
    result = cli.run(["service", "scale", f"{service_name}={replicas}"])
    if not result.ok:
        raise RuntimeError(result.stderr or "调整副本数失败")
    return {"message": "服务扩缩容命令已提交", "output": result.stdout}


def force_restart_service(service_name: str) -> dict:
    cli = DockerCLI()
    result = cli.run(["service", "update", "--force", service_name])
    if not result.ok:
        raise RuntimeError(result.stderr or "强制重启服务失败")
    return {"message": "服务重启命令已提交", "output": result.stdout}
