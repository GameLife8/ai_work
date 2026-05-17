"""K8s pod 应用日志查看（薄封装，给模型清晰引导）。

为什么保留这个 skill 而不是直接用 kube_query
============================================
``kube_query(verb=logs, ...)`` 已经完全支持 ``--previous`` / ``--tail`` / ``--since``
/ ``-c container`` 等参数。本 skill **不重复实现逻辑**，内部就是 kube_query
logs 路径的别名，存在的价值是**给模型一段集中、详细的引导**——多容器场景、
CrashLoopBackOff 时必须 ``previous=True``、tail 调多大合适等经验，
都写在 MANIFEST.description 里，让模型一眼就知道怎么用。

跟 kube_query 一样会自动走 scanners.k8s_logs 识别 OOM / DNS / 超时 / 权限错。
"""

from __future__ import annotations

from ops_platform.scanners import k8s_logs as scan_logs
from ops_platform.signals import attach


MANIFEST = {
    "code": "k8s_get_pod_logs",
    "name": "拉取 Pod 日志（带 CrashLoop 引导）",
    "description": (
        "拉取 pod 应用层日志（``kubectl logs --tail=N``）。等价于 ``kube_query(verb=logs, name=...)``"
        "但参数语义清晰、引导集中，**优先用本 skill**。"
        "**关键技巧**："
        "  - CrashLoopBackOff 时**必须**带 ``previous=True``——否则只能看到刚启动还没崩的日志；"
        "  - 多容器 pod 必须用 ``container`` 参数指定，否则可能拉到不相关 sidecar 的日志；"
        "  - 怀疑是某个时间窗口的问题用 ``since=5m`` / ``since=1h``；"
        "  - 默认 tail=200 一般够；调到 1000+ 谨慎，模型上下文会被刷掉。"
        "**返回的 ``_signals`` 会扫日志关键词自动 pivot**：OOMKilled / 端口拒绝 / 超时 / 权限错误时建议下一步。"
        "如果日志一切正常但 pod 仍异常，说明问题在系统层（OOM/scheduling/资源），回去调 ``kube_query(verb=describe, resource=pod, name=...)``。"
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
            "tail": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
            "since": {"type": "string", "description": "如 5m / 1h / 2025-01-01T00:00:00Z"},
            "previous": {"type": "boolean", "default": False,
                         "description": "CrashLoop 必开——看上次崩溃前的日志"},
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
    result = client.get_pod_logs(
        name, namespace=namespace, container=container,
        tail=tail, since=since, previous=previous,
    )
    log_text = ""
    if isinstance(result, dict):
        log_text = (
            result.get("logs") or result.get("stdout") or result.get("text") or ""
        )
    sigs = scan_logs.scan(log_text, pod=name, namespace=namespace)
    return attach(result, sigs) if isinstance(result, dict) else result
