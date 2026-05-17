r"""宿主机通用只读查询 skill —— 一把口替代所有 ss/ip/iptables-save/dmesg/df/lsof/find …

设计要点
========
1. **read_only=True，不需 admin 审批**——只读查询，安全边界靠 agent ``allowed.yml``
   白名单（已硬约束）+ 本 skill 层 binary 白名单 + 禁 shell 元字符。
2. **binary 白名单**：再额外做一层平台侧约束，**禁掉 sh/bash 等** shell。理由：
   agent ``allowed.yml`` 给 ``host_run_command`` 留了 sh/bash（让 admin 审批通过的
   pipe 能跑），但本 skill 是无审批通道，绝不能让模型用 ``sh -c "rm -rf /"`` 之类。
3. **禁 shell 元字符**：``; | & ` $ < > $() 反引号 换行`` 一律拒绝。模型要 pipe / 重定向
   就走 ``host_run_command``（admin 审批）。
4. **scanner 自动路由**：识别 dmesg / iptables-save 类输出，attach 信号；其他命令
   不发信号也不出错。
"""

from __future__ import annotations

import shlex

from ops_platform.scanners import host_iptables as scan_iptables
from ops_platform.scanners import host_kernel as scan_kernel
from ops_platform.signals import attach


# 只读 binary 白名单——跟 agent allowed.yml 一致，但**剔除 sh/bash**
# 防止 host_query 被滥用成"无审批 RPC"。
ALLOWED_BINARIES = frozenset({
    # 网络 socket / 路由 / 接口
    "ss", "ip", "ipvsadm", "iptables-save", "ip6tables-save",
    "nft", "ethtool", "bridge", "arp",
    # 包抓取（短时 + tail 限制）
    "tcpdump",
    # 内核 / 系统事件
    "dmesg", "sysctl", "lsof",
    "lscpu", "lsblk", "lsmod",
    "ps", "top", "free", "uptime",
    "vmstat", "iostat", "mpstat",
    # 文件读取（path 收口，agent 端按 read-only 挂载）
    "cat", "head", "tail", "stat", "file",
    "find", "df", "du", "mount",
    # 容器排障（K8s 节点）
    "crictl", "ctr",
    # DNS / 域名解析
    "dig", "nslookup", "host", "getent",
})

# Shell 元字符（除空格外）——拒绝，防注入
_BAD_CHARS = set(";|&`$<>\n\r\t\"'")
# 反斜杠转义、`$(`、`${` 等也要挡
_BAD_PATTERNS = ("$(", "${", "`", "\\$", "\\`", "&&", "||", ">>", "<<", "2>")


MANIFEST = {
    "code": "host_query",
    "name": "宿主机只读查询",
    "description": (
        "**宿主机只读取证的唯一入口**——在指定节点的宿主机 namespace 执行白名单内的只读命令。"
        "**不需要审批**（read_only），安全边界：(1) agent allowed.yml；(2) 本 skill binary 白名单；(3) 禁 shell 元字符。"
        "白名单：ss / ip / iptables-save / ip6tables-save / nft / ipvsadm / ethtool / bridge / arp / "
        "tcpdump / dmesg / sysctl / lsof / lscpu / lsblk / lsmod / ps / top / free / uptime / vmstat / iostat / mpstat / "
        "cat / head / tail / stat / file / find / df / du / mount / crictl / ctr / dig / nslookup / host / getent。"
        "**禁用** sh / bash / pipe / 重定向 / 反引号——要这些请走 host_run_command（admin 审批）或 host_run_command_async（长命令）。"
        "示例："
        "  - 看监听端口：``command='ss -ltnup'``"
        "  - 看路由：``command='ip route'``"
        "  - 看防火墙：``command='iptables-save'``  （可加 probe_port=N 让 scanner 检查端口是否被拦）"
        "  - 看内核事件：``command='dmesg --time-format iso'``"
        "  - 看磁盘：``command='df -h'``"
        "  - 看 conntrack：``command='cat /proc/net/nf_conntrack'``"
        "  - DNS 排查：``command='dig coredns.kube-system.svc.cluster.local'``"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "command": {"type": "string",
                        "description": "要执行的命令；binary 必须在白名单内，禁 shell 元字符"},
            "namespaces": {"type": "string", "default": "muinp",
                           "description": "nsenter 进入的 ns：m/u/i/n/p/U/C；默认 muinp"},
            "probe_port": {"type": "integer", "minimum": 1, "maximum": 65535,
                           "description": "可选；仅 iptables-save / nft 类命令时——让 scanner 检查端口是否被拦"},
            "connection_id": {"type": "string"},
        },
        "required": ["node", "command"],
    },
}


def _validate_command(command: str) -> list[str]:
    """把 command 字符串切成 argv，做三重校验：

    1. shlex.split 解析（自动认引号）
    2. 拒绝 shell 元字符 / 复合表达式
    3. 首个 token 必须是白名单 binary
    """
    if not command or not command.strip():
        raise ValueError("command 不能为空")
    # 元字符快速检查（在 shlex 之前，因为 shlex 会把它们当字面量保留）
    for c in _BAD_CHARS:
        if c in command:
            raise ValueError(f"命令含禁用字符 {c!r}：要管道/重定向请走 host_run_command (admin 审批)")
    for p in _BAD_PATTERNS:
        if p in command:
            raise ValueError(f"命令含禁用模式 {p!r}")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"command 解析失败：{exc}")
    if not argv:
        raise ValueError("command 为空")
    binary = argv[0]
    if binary not in ALLOWED_BINARIES:
        raise ValueError(
            f"binary {binary!r} 不在 host_query 白名单内。"
            f"如需运行该命令，走 host_run_command（admin 审批）。"
            f"当前白名单 binary 见 MANIFEST.description。"
        )
    return argv


def _route_scanner(*, node: str, argv: list[str], stdout: str,
                   probe_port: int | None) -> list[dict]:
    if not stdout:
        return []
    binary = argv[0]
    if binary == "dmesg":
        return scan_kernel.scan(node, stdout)
    if binary in ("iptables-save", "ip6tables-save"):
        return scan_iptables.scan(node, stdout, probe_port=probe_port, mode="iptables")
    if binary == "nft":
        return scan_iptables.scan(node, stdout, probe_port=probe_port, mode="nft")
    return []


def run(
    ctx,
    *,
    node: str,
    command: str,
    namespaces: str = "muinp",
    probe_port: int | None = None,
    connection_id: str | None = None,
) -> dict:
    argv = _validate_command(command)
    client = ctx.connection_for("host_agent", connection_id)

    ns_tuple = tuple(c for c in namespaces if c in {"m", "u", "i", "n", "p", "U", "C"})
    ns_tuple = ns_tuple or ("m", "u", "i", "n", "p")
    result = client.nsenter_on_node(node, argv, namespaces=ns_tuple)

    payload = {
        "node": node,
        "command": command,
        "argv": argv,
        "namespaces": namespaces,
        "ok": result.ok,
        "stdout": (result.stdout or "")[-32000:],
        "stderr": (result.stderr or "")[-4000:],
        "returncode": result.returncode,
        "transport": getattr(result, "transport", ""),
    }
    sigs = _route_scanner(node=node, argv=argv, stdout=result.stdout, probe_port=probe_port)
    return attach(payload, sigs)
