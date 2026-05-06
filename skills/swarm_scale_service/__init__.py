"""扩缩容 Swarm 服务副本数。

执行 ``docker service scale <name>=<replicas>``。属于写操作，需用户确认。
"""

from __future__ import annotations

MANIFEST = {
    "code": "swarm_scale_service",
    "name": "扩缩容 Swarm 服务",
    "description": (
        "调整某个 Swarm 服务的副本数；底层执行 ``docker service scale name=N``。"
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
            "replicas": {"type": "integer", "minimum": 0, "maximum": 200},
            "connection_id": {"type": "string"},
        },
        "required": ["service_name", "replicas"],
    },
}


def run(ctx, *, service_name: str, replicas: int, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("swarm", connection_id)
    result = client.run(["service", "scale", f"{service_name}={int(replicas)}"])
    return {
        "service_name": service_name,
        "replicas": int(replicas),
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
