"""在宿主机网络 namespace 中导出防火墙规则（iptables / nft）。"""

from __future__ import annotations

import re

from ops_platform.signals import (
    SEV_CRITICAL,
    SEV_WARNING,
    SIG_PORT_BLOCKED,
    attach,
    signal,
)


MANIFEST = {
    "code": "host_iptables_dump",
    "name": "宿主机防火墙规则导出",
    "description": (
        "在指定节点的宿主机网络 namespace 中导出 iptables / nftables 规则全量，"
        "用于排查「端口被拦 / DNAT 规则错 / kube-proxy iptables 模式异常 / Swarm ingress 规则丢失」。"
        "默认尝试 ``iptables-save``，没有就退到 ``nft list ruleset``。"
        "可选 ``mode``：iptables / nft / ipvs。"
        "**新**：可选 ``probe_port=N``——agent 扫规则文本里有没有 DROP/REJECT 命中"
        "这个端口，命中就 emit ``port_blocked_by_iptables`` 信号，让 agent 自动 pivot 到"
        "host_socket_overview 看端口实际监听情况、对比规则是否误拦。"
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
            "probe_port": {
                "type": "integer",
                "description": (
                    "可选；端口号。如果传了，扫规则全文找 ``--dport <port>`` 行带 "
                    "DROP/REJECT 的，命中就发 SIG_PORT_BLOCKED 提示 pivot 到 socket_overview。"
                ),
                "minimum": 1, "maximum": 65535,
            },
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


# iptables-save 输出一行例：
#   -A INPUT -p tcp -m tcp --dport 9100 -j DROP
#   -A FORWARD -p tcp -m tcp --dport 6443 -j REJECT --reject-with icmp-port-unreachable
# nft 输出例：
#   tcp dport 6443 drop
_RE_IPTABLES_BLOCK = re.compile(
    r"--dport\s+(?P<port>\d+).*\s+-j\s+(?P<verdict>DROP|REJECT)",
    re.IGNORECASE,
)
_RE_NFT_BLOCK = re.compile(
    r"\bdport\s+(?P<port>\d+).*?\b(?P<verdict>drop|reject)\b",
    re.IGNORECASE,
)


def _scan_port_blocks(text: str, port: int, mode: str) -> list[str]:
    """返回拦截 `port` 的规则行（match.group(0)），最多 8 行。"""
    if not text:
        return []
    hits: list[str] = []
    re_pattern = _RE_NFT_BLOCK if mode == "nft" else _RE_IPTABLES_BLOCK
    for line in text.splitlines():
        m = re_pattern.search(line)
        if m and int(m["port"]) == int(port):
            hits.append(line.strip())
            if len(hits) >= 8:
                break
    return hits


def _extract_signals(*, node: str, port: int | None, hits: list[str]) -> list[dict]:
    if not port or not hits:
        return []
    sample = (hits[0] or "")[:200]
    return [signal(
        SIG_PORT_BLOCKED,
        severity=SEV_CRITICAL,
        evidence=(
            f"节点 {node} 防火墙规则中检测到 ``--dport {port}`` 的 DROP/REJECT 规则 "
            f"（命中 {len(hits)} 条）。示例：``{sample}``"
        ),
        next_skill="host_socket_overview",
        next_args={"node": node, "filter": f":{port}"},
        context={"node": node, "port": int(port), "rule_count": len(hits)},
    )]


def run(
    ctx,
    *,
    node: str,
    mode: str = "iptables",
    probe_port: int | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    if mode == "nft":
        inner = ["nft", "list", "ruleset"]
    elif mode == "ipvs":
        inner = ["ipvsadm", "-L", "-n"]
    else:
        inner = ["iptables-save"]
    result = client.nsenter_on_node(node, inner)

    payload = {**result.to_dict(), "mode": mode}

    # 如果用户传了 probe_port，扫规则发 signal（ipvs 模式不适用）
    if probe_port and mode in ("iptables", "nft"):
        hits = _scan_port_blocks(result.stdout, int(probe_port), mode)
        payload["probe_port"] = int(probe_port)
        payload["blocking_rules"] = hits
        sigs = _extract_signals(node=node, port=probe_port, hits=hits)
        return attach(payload, sigs)

    return payload
