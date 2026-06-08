"""扫 iptables-save / nft list ruleset 输出。

接收 ``probe_port``——如果传了，匹配 ``--dport <port>`` 命中 DROP/REJECT 的行，
emit ``port_blocked_by_iptables`` 信号让模型 pivot 到 host_run_command(command='ss -ltnup')
对照监听。
"""

from __future__ import annotations

import re

from ops_platform.signals import (
    SEV_CRITICAL,
    SIG_PORT_BLOCKED,
    signal,
)


_RE_IPTABLES_BLOCK = re.compile(
    r"--dport\s+(?P<port>\d+).*\s+-j\s+(?P<verdict>DROP|REJECT)",
    re.IGNORECASE,
)
_RE_NFT_BLOCK = re.compile(
    r"\bdport\s+(?P<port>\d+).*?\b(?P<verdict>drop|reject)\b",
    re.IGNORECASE,
)


def scan(node: str, text: str, *, probe_port: int | None = None, mode: str = "iptables") -> list[dict]:
    if not probe_port or not text:
        return []
    re_pattern = _RE_NFT_BLOCK if mode == "nft" else _RE_IPTABLES_BLOCK
    hits: list[str] = []
    for line in text.splitlines():
        m = re_pattern.search(line)
        if m and int(m["port"]) == int(probe_port):
            hits.append(line.strip())
            if len(hits) >= 8:
                break
    if not hits:
        return []
    sample = (hits[0] or "")[:200]
    return [signal(
        SIG_PORT_BLOCKED, severity=SEV_CRITICAL,
        evidence=(
            f"节点 {node} 防火墙规则中检测到 ``--dport {probe_port}`` 的 DROP/REJECT 规则 "
            f"（命中 {len(hits)} 条）。示例：``{sample}``"
        ),
        next_skill="host_run_command",
        next_args={"node": node, "command": f"ss -ltnup"},
        context={"node": node, "port": int(probe_port), "rule_count": len(hits),
                 "hint": f"对比 ss 看 :{probe_port} 是否真的有进程在监听"},
    )]
