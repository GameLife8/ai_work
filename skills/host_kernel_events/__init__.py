"""宿主机内核事件（dmesg）—— 薄包装 + 老节点 dmesg 兼容性 fallback。

为什么保留这个 skill 而不是直接用 host_query
============================================
``host_query(command='dmesg --time-format iso')`` 在 CentOS 7 / SLES 12 这类
util-linux < 2.30 的老节点上**会直接报错**——这些节点的 dmesg 不识别
``--time-format`` flag。

模型不知道这种兼容性细节。本 skill 封装三段降级：
  1. ``dmesg --time-format iso --ctime``  (util-linux ≥ 2.30 / CentOS 8+)
  2. ``dmesg -T``                         (util-linux ≥ 2.20 / CentOS 7)
  3. ``dmesg``                            (任何版本，无时间戳)

每步失败就降级一次。配合 ``scanners.host_kernel`` 自动识别 OOM killer / conntrack
table full / 磁盘 I/O 错误。

这是 host_query 体系**唯一保留的薄包装**，理由：兼容性逻辑无法靠 description 引导。
"""

from __future__ import annotations

from ops_platform.scanners import host_kernel as scan_kernel
from ops_platform.signals import attach


MANIFEST = {
    "code": "host_kernel_events",
    "name": "宿主机内核事件（带老节点兼容）",
    "description": (
        "拉取节点 ``dmesg`` 最近 N 行（默认 200），可选关键词过滤。**自动处理老 CentOS 7 / "
        "SLES 12 的 dmesg flag 兼容**：先试 ``--time-format iso --ctime``，失败降到 ``-T``，"
        "再失败就用裸 ``dmesg``。"
        "**用于发现**：OOM killer / conntrack table full / network drop / 文件系统 I/O 错误 / "
        "TCP 限流 / iptables nf_table 警告 / NFS RPC error 等。"
        "**关键词建议**：oom / conntrack / drop / blocked / nfs / xfs / ext4 / nf_table。"
        "返回的 ``_signals`` 自动识别 OOM / conntrack 满 / 磁盘 I/O 错误并 pivot。"
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


def run(
    ctx,
    *,
    node: str,
    tail: int = 200,
    keyword: str | None = None,
    connection_id: str | None = None,
) -> dict:
    """三段式 fallback 兼容老 dmesg。"""
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
        err_low = (result.stderr or "").lower()
        if not any(t in err_low for t in (
            "unrecognized option", "invalid option", "unknown option",
        )):
            # 非 flag 兼容性问题，没必要继续降级
            used_cmd = " ".join(cmd)
            break
        used_cmd = " ".join(cmd)

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
    return attach(payload, scan_kernel.scan(node, events))
