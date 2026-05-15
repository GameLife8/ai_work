from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CONNTRACK_FULL,
    SIG_DISK_IO_ERROR,
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
        # 降级为 warning：conntrack 满通常是瞬时高并发，几秒内自愈，不算 critical 故障
        # （要做 critical 升级需要看是否持续命中 / 是否有 dropped 包统计）
        sigs.append(signal(
            SIG_CONNTRACK_FULL, severity=SEV_WARNING,
            evidence=f"节点 {node} conntrack 表满（nf_conntrack: table full），新连接将被丢",
            next_skill="host_iptables_dump",
            next_args={"node": node},
            context={"node": node},
        ))
    if "i/o error" in low or "ata.*error" in low or "blk_update_request" in low:
        # 磁盘 IO 错误保持 critical —— 硬件层面警示
        sigs.append(signal(
            SIG_DISK_IO_ERROR, severity=SEV_CRITICAL,
            evidence=f"节点 {node} 内核报磁盘 I/O 错误，硬件层面有风险",
            next_skill="zabbix_get_host_storage_overview",
            next_args={"host_query": node},
            context={"node": node},
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
    """三段式 fallback 兼容老 dmesg：
       1. ``dmesg --time-format iso --ctime``  (util-linux ≥ 2.30，CentOS 8+)
       2. ``dmesg -T``                          (util-linux ≥ 2.20，CentOS 7)
       3. ``dmesg``                              (任何版本，无时间戳)
    每次失败就降级一次，避免老节点（CentOS 7 / SLES 12）直接挂掉。
    """
    client = ctx.connection_for("host_agent", connection_id)

    attempts = (
        ["dmesg", "--time-format", "iso", "--ctime"],
        ["dmesg", "-T"],
        ["dmesg"],
    )
    result = None
    used_cmd = None
    for cmd in attempts:
        result = client.nsenter_on_node(node, cmd)
        if result.ok and result.stdout:
            used_cmd = " ".join(cmd)
            break
        # stderr 出现 unrecognized option / invalid option / unknown 就降级
        err_low = (result.stderr or "").lower()
        if not any(t in err_low for t in (
            "unrecognized option", "invalid option", "unknown option",
        )):
            # 不是 flag 兼容性问题，没必要再降级
            used_cmd = " ".join(cmd)
            break
        used_cmd = " ".join(cmd)   # 记最后一次尝试的 cmd

    lines = (result.stdout if result else "").splitlines()
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
        "dmesg_command": used_cmd,
        "ok": bool(result and result.ok),
        "stderr": (result.stderr if result else "")[:2000],
    }
    return attach(payload, _extract_kernel_signals(node, events))
