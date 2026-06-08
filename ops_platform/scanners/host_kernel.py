"""扫宿主机内核日志（``dmesg`` 输出）。

跟原 host_kernel_events 内置 _extract_kernel_signals 同语义。
"""

from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CONNTRACK_FULL, SIG_DISK_IO_ERROR, SIG_OOM_LOG,
    signal,
)


def scan(node: str, events_text: str) -> list[dict]:
    sigs: list[dict] = []
    low = (events_text or "").lower()
    if "out of memory" in low or "killed process" in low or "invoked oom-killer" in low:
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
            SIG_CONNTRACK_FULL, severity=SEV_WARNING,
            evidence=f"节点 {node} conntrack 表满（nf_conntrack: table full），新连接将被丢",
            next_skill="host_run_command",
            next_args={"node": node, "command": "iptables-save"},
            context={"node": node},
        ))
    if "i/o error" in low or "ata.*error" in low or "blk_update_request" in low:
        sigs.append(signal(
            SIG_DISK_IO_ERROR, severity=SEV_CRITICAL,
            evidence=f"节点 {node} 内核报磁盘 I/O 错误，硬件层面有风险",
            next_skill="zabbix_get_host_storage_overview",
            next_args={"host_query": node},
            context={"node": node},
        ))
    return sigs
