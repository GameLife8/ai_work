"""扩缩容 Deployment（写操作）。"""

from __future__ import annotations

MANIFEST = {
    "code": "k8s_scale_deployment",
    "name": "扩缩容 K8s Deployment",
    "description": (
        "调整 Deployment 副本数（``kubectl scale deployment/<name> --replicas=N``）。"
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
            "replicas": {"type": "integer", "minimum": 0, "maximum": 200},
            "connection_id": {"type": "string"},
        },
        "required": ["name", "replicas"],
    },
}


def run(
    ctx,
    *,
    name: str,
    replicas: int,
    namespace: str | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("k8s", connection_id)
    result = client.scale_deployment(name, int(replicas), namespace=namespace)
    return {
        "name": name,
        "namespace": namespace or client.namespace,
        "replicas": int(replicas),
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
