"""在宿主机网络 namespace 中导出防火墙规则（iptables / nft）。"""

from __future__ import annotations

MANIFEST = {
    "code": "host_iptables_dump",
    "name": "宿主机防火墙规则导出",
    "description": (
        "在指定节点的宿主机网络 namespace 中导出 iptables / nftables 规则全量，"
        "用于排查「端口被拦 / DNAT 规则错 / kube-proxy iptables 模式异常 / Swarm ingress 规则丢失」。"
        "默认尝试 ``iptables-save``，没有就退到 ``nft list ruleset``。"
        "可选 ``mode``：iptables / nft / ipvs。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "mode": {"type": "string", "enum": ["iptables", "nft", "ipvs"], "default": "iptables"},
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


def run(ctx, *, node: str, mode: str = "iptables", connection_id: str | None = None) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    if mode == "nft":
        inner = ["nft", "list", "ruleset"]
    elif mode == "ipvs":
        inner = ["ipvsadm", "-L", "-n"]
    else:
        inner = ["iptables-save"]
    result = client.nsenter_on_node(node, inner)
    return {**result.to_dict(), "mode": mode}
