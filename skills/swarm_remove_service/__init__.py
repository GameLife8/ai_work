"""删除一个 Swarm 服务。

执行 ``docker service rm <service_name>``。**高危操作**，需 admin 确认。
"""

from __future__ import annotations

MANIFEST = {
    "code": "swarm_remove_service",
    "name": "删除 Swarm 服务",
    "description": (
        "删除某个 Swarm 服务（``docker service rm``），所有副本会被立即下线。"
        "属于高危写操作,当前会话用户(admin)审批后执行。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": False,
    # ``visibility=admin`` 已经把访问限制在 admin 角色——审批人就是 admin 自己,
    # 不再需要 ``requires_admin_approval=True`` 自己审自己(语义冗余)。当前会话用户点确认即执行。
    "requires_admin_approval": False,
    "visibility": "admin",
    "confirmation_ttl_seconds": 600,
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
    result = client.run(["service", "rm", service_name])
    return {
        "service_name": service_name,
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
