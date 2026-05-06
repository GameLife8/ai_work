"""宿主机路由 + 接口 + 网络 namespace 概览。"""

from __future__ import annotations

import shlex

MANIFEST = {
    "code": "host_route_overview",
    "name": "宿主机网络栈概览",
    "description": (
        "一次性返回指定节点宿主机的：``ip a``（接口）+ ``ip route show table all``（全表路由）"
        "+ ``ip rule``（策略路由）+ ``ip netns list``（所有网络 namespace）。"
        "用于排查「路由黑洞 / 多网卡选错 / vxlan/overlay 接口异常 / 网络 namespace 数量异常」。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


def run(ctx, *, node: str, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    sections = [
        ("interfaces", ["ip", "-c=never", "addr"]),
        ("routes",     ["ip", "-c=never", "route", "show", "table", "all"]),
        ("rules",      ["ip", "-c=never", "rule"]),
        ("netns",      ["ip", "netns", "list"]),
    ]
    out: dict[str, str] = {}
    for key, cmd in sections:
        result = client.nsenter_on_node(node, cmd)
        out[key] = result.stdout if result.ok else f"[error] {result.stderr or result.stdout}"
    return {"node": node, "sections": out}
