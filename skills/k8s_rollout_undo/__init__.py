"""回滚 Deployment 到上一个版本（写操作）。"""

from __future__ import annotations

MANIFEST = {
    "code": "k8s_rollout_undo",
    "name": "回滚 K8s Deployment",
    "description": (
        "将 Deployment 回滚到上一版本（``kubectl rollout undo deployment/<name>``）。"
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
    result = client.rollout_undo(name, namespace=namespace)
    return {
        "name": name,
        "namespace": namespace or client.namespace,
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
