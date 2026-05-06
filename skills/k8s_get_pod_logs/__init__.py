from __future__ import annotations

MANIFEST = {
    "code": "k8s_get_pod_logs",
    "name": "拉取 Pod 日志",
    "description": (
        "拉取 pod 应用层日志（``kubectl logs --tail=N``）。"
        "**关键技巧**："
        "  - CrashLoopBackOff 时**必须**带 ``previous=True``——否则只能看到刚启动还没崩的日志；"
        "  - 多容器 pod 必须用 ``container`` 参数指定，否则可能拉到不相关 sidecar 的日志；"
        "  - 怀疑是某个时间窗口的问题用 ``since=5m`` / ``since=1h``；"
        "  - 默认 tail=200 一般够；调到 1000+ 谨慎，模型上下文会被刷掉。"
        "如果日志一切正常但 pod 仍异常，说明问题在系统层（OOM/scheduling/资源），回去调 k8s_describe_pod。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "namespace": {"type": "string"},
            "container": {"type": "string"},
            "tail": {"type": "integer"},
            "since": {"type": "string", "description": "如 5m / 1h / 2025-01-01T00:00:00Z"},
            "previous": {"type": "boolean"},
            "connection_id": {"type": "string"},
        },
        "required": ["name"],
    },
}


def run(
    ctx,
    *,
    name: str,
    namespace: str | None = None,
    container: str | None = None,
    tail: int | None = None,
    since: str | None = None,
    previous: bool = False,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("k8s", connection_id)
    return client.get_pod_logs(
        name,
        namespace=namespace,
        container=container,
        tail=tail,
        since=since,
        previous=previous,
    )
