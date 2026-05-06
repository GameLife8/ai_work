from __future__ import annotations

MANIFEST = {
    "code": "swarm_list_services",
    "name": "列出 Swarm 服务",
    "description": "列出 Docker Swarm 服务，适合先确认服务是否存在、当前副本是否正常。",
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "filter_name": {"type": "string"},
            "connection_id": {"type": "string", "description": "可选；指定要操作的 Swarm 集群"},
        },
    },
}


def run(ctx, *, filter_name: str | None = None, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("swarm", connection_id)
    return client.list_services(filter_name)
