from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_OOM_LOG,
    attach, signal,
)

MANIFEST = {
    "code": "host_kernel_events",
    "name": "宿主机内核事件",
    "description": (
        "拉取节点的 ``dmesg`` 最近 N 行（默认 200），可选关键词过滤。"
        "用于发现：OOM killer / conntrack table full / network drop / 文件系统 I/O 错误 / "
        "TCP 限流（tcp_collapse） / iptables nf_table 警告 / NFS RPC error 等。"
        "**关键词建议**：oom / conntrack / drop / blocked / nfs / xfs / ext4 / nf_table。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "tail": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
            "keyword": {"type": "string", "description": "可选；对输出做大小写不敏感 grep"},
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


def _extract_kernel_signals(node: str, events_text: str) -> list[dict]:
    sigs: list[dict] = []
    low = (events_text or "").lower()
    if "out of memory" in low or "killed process" in low or "invoked oom-killer" in low:
        # 抓一行作为证据
        sample = ""
        for line in events_text.splitlines():
            if "out of memory" in line.lower() or "killed process" in line.lower():
                sample = line.strip()
                break
        sigs.append(signal(
            SIG_OOM_LOG, severity=SEV_CRITICAL,
            evidence=f"节点 {node} 内核日志中检测到 OOM killer 事件：{sample[:160]}",
            next_skill="zabbix_get_host_overview",
            next_args={"host_query": node},
            context={"node": node},
        ))
    if "nf_conntrack: table full" in low:
        sigs.append(signal(
            "conntrack_table_full", severity=SEV_CRITICAL,
            evidence=f"节点 {node} conntrack 表满，将丢弃新连接",
            next_skill="host_iptables_dump",
            next_args={"node": node},
        ))
    if "i/o error" in low or "ata.*error" in low or "blk_update_request" in low:
        sigs.append(signal(
            "disk_io_error", severity=SEV_CRITICAL,
            evidence=f"节点 {node} 内核报磁盘 I/O 错误，硬件层面有风险",
            next_skill="zabbix_get_host_storage_overview",
            next_args={"host_query": node},
        ))
    return sigs


def run(
    ctx,
    *,
    node: str,
    tail: int = 200,
    keyword: str | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    inner = ["dmesg", "--time-format", "iso", "--ctime"]
    result = client.nsenter_on_node(node, inner)
    lines = result.stdout.splitlines()
    if keyword:
        lines = [ln for ln in lines if keyword.lower() in ln.lower()]
    lines = lines[-int(tail):]
    events = "\n".join(lines)
    payload = {
        "node": node,
        "tail": int(tail),
        "keyword": keyword,
        "matched_count": len(lines),
        "events": events,
        "ok": result.ok,
        "stderr": result.stderr,
    }
    return attach(payload, _extract_kernel_signals(node, events))
