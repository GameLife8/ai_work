from __future__ import annotations

MANIFEST = {
    "code": "swarm_get_service_status",
    "name": "查看 Swarm 服务状态",
    "description": "查看服务当前状态和副本情况。",
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": True,
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
    return ctx.connection_for("swarm", connection_id).get_service_status(service_name)
