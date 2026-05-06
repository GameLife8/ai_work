from __future__ import annotations

MANIFEST = {
    "code": "zabbix_get_host_overview",
    "name": "主机概览",
    "description": (
        "获取主机概览：CPU 用率、内存用率、agent 可用性、近 1h 趋势。"
        "**两类典型场景**："
        "(1) 用户直接问「主机当前状态」——直接调；"
        "(2) 容器/Pod 排障时把 swarm task.Node / k8s pod.node 当作 host_query 反查——验证容器异常是不是宿主机扛不住引起的。"
        "如果 CPU 持续 >80% 或内存 >90%，往往就是导致容器 OOM/重启的根因；"
        "如果 agent 不可用，主机本身可能宕机或网络隔离，需要走另一条排查路径。"
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
    return ctx.connection_for("zabbix", connection_id).get_host_overview(host_query)
