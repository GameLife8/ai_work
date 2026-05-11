"""强制重启 / 重新部署一个 Swarm 服务（写操作示例）。

执行 ``docker service update --force <name>``：滚动重启所有副本，常用于：
- 镜像 tag 没变但希望重新拉一次
- 把卡死的副本踢一遍
- 应用新挂载/新 secret 后让服务感知
"""

from __future__ import annotations

MANIFEST = {
    "code": "swarm_force_update_service",
    "name": "强制更新 Swarm 服务",
    "description": (
        "对指定 Swarm 服务执行 ``docker service update --force``，触发**滚动重启所有副本**。"
        "副本数少 / 无 HA 的关键服务调用会出现短暂可用性下降——跟 remove/scale 一致走 admin 审批。"
        "属于**需 admin 审批**的写操作；普通用户可以发起，但只有 admin 能确认执行。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": False,
    # 跟 swarm_remove_service / swarm_rollback_service 对齐：触发滚动重启的写操作走 admin 审批
    "requires_admin_approval": True,
    "visibility": "all",
    "confirmation_ttl_seconds": 300,
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
    result = client.run(["service", "update", "--force", service_name])
    return {
        "service_name": service_name,
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
