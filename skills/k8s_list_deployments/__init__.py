from __future__ import annotations

MANIFEST = {
    "code": "k8s_list_deployments",
    "name": "列出 K8s Deployment",
    "description": "列出指定 namespace 下的 Deployment，含期望/就绪/可用副本和当前镜像。",
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "connection_id": {"type": "string"},
        },
    },
}


def run(ctx, *, namespace: str | None = None, connection_id: str | None = None) -> dict:
    return ctx.connection_for("k8s", connection_id).list_deployments(namespace=namespace)
