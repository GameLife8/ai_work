from __future__ import annotations

MANIFEST = {
    "code": "zabbix_get_host_storage_overview",
    "name": "主机磁盘概览",
    "description": (
        "获取主机所有挂载点的容量 / 剩余 / 使用率。"
        "**触发场景**："
        "(1) 用户直接问磁盘情况——直接调；"
        "(2) 容器日志/任务里出现 'no space left' / 'write failed' / 'disk pressure' / Evicted——"
        "    把出问题容器所在的 Node 名当 host_query 反查，定位是哪个挂载点满了；"
        "(3) K8s pod 被 Evicted with DiskPressure——同样反查节点。"
        "返回里使用率 >90% 的挂载点要重点提，建议用户清理或扩容。"
    ),
    "category": "zabbix",
    "required_connection_type": "zabbix",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "host_query": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["host_query"],
    },
}


def run(ctx, *, host_query: str, connection_id: str | None = None) -> dict:
    return ctx.connection_for("zabbix", connection_id).get_host_storage_overview(host_query)
