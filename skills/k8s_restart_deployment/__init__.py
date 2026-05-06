"""触发 Deployment 滚动重启（写操作）。

执行 ``kubectl rollout restart deployment/<name>``。
"""

from __future__ import annotations

MANIFEST = {
    "code": "k8s_restart_deployment",
    "name": "重启 K8s Deployment",
    "description": (
        "对指定 K8s Deployment 触发滚动重启（``kubectl rollout restart deployment/<name>``）。"
        "属于写操作，需用户确认。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": False,
    "requires_admin_approval": False,
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
