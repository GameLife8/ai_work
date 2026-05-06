"""把 Swarm 服务的镜像升级 / 切换到指定 tag。

执行 ``docker service update --image <new_image> <service_name>``。
属于写操作，需用户确认。
"""

from __future__ import annotations

MANIFEST = {
    "code": "swarm_update_service_image",
    "name": "更新 Swarm 服务镜像",
    "description": (
        "把指定 Swarm 服务切换到新的镜像 tag（滚动升级）。"
        "底层执行 ``docker service update --image <image> <service_name>``。"
        "属于写操作，需用户确认。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": False,
    "requires_admin_approval": False,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "image": {"type": "string", "description": "完整镜像引用，如 registry/foo:1.2.3"},
            "with_registry_auth": {"type": "boolean", "default": True},
            "connection_id": {"type": "string"},
        },
        "required": ["service_name", "image"],
    },
}


def run(
    ctx,
    *,
    service_name: str,
    image: str,
    with_registry_auth: bool = True,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("swarm", connection_id)
    cmd = ["service", "update", "--image", image]
    if with_registry_auth:
        cmd.append("--with-registry-auth")
    cmd.append(service_name)
    result = client.run(cmd)
    return {
        "service_name": service_name,
        "image": image,
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
