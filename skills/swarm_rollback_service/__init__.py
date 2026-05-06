"""把 Swarm 服务回滚到上一个版本。

执行 ``docker service rollback <service_name>``。属于写操作，需用户确认。
"""

from __future__ import annotations

MANIFEST = {
    "code": "swarm_rollback_service",
    "name": "回滚 Swarm 服务",
    "description": (
        "把指定 Swarm 服务回滚到上一个版本；底层执行 ``docker service rollback``。"
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
            "connection_id": {"type": "string"},
        },
        "required": ["service_name"],
    },
}


def run(ctx, *, service_name: str, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("swarm", connection_id)
    result = client.run(["service", "rollback", service_name])
    return {
        "service_name": service_name,
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
