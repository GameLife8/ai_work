"""触发 Deployment 滚动重启（写操作）。

执行 ``kubectl rollout restart deployment/<name>``。
"""

from __future__ import annotations

MANIFEST = {
    "code": "k8s_restart_deployment",
    "name": "重启 K8s Deployment",
    "description": (
        "对指定 K8s Deployment 触发滚动重启（``kubectl rollout restart deployment/<name>``）。"
        "**会让所有 Pod 顺序被新副本替换**——副本数少 / 没配 PDB / 单副本服务调用本 skill 会出现短暂可用性下降。"
        "属于**需 admin 审批**的写操作；普通用户可以发起，但只有 admin 能确认执行。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": False,
    # 滚动重启在副本数少 / 无 PDB 时仍可能造成可用性下降——跟 scale/rollback 一致走 admin 审批
    "requires_admin_approval": True,
    "confirmation_ttl_seconds": 300,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "namespace": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["name"],
    },
}


def run(ctx, *, name: str, namespace: str | None = None, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("k8s", connection_id)
    result = client.restart_deployment(name, namespace=namespace)
    return {
        "name": name,
        "namespace": namespace or client.namespace,
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
