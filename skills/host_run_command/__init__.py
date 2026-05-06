"""在宿主机 namespace 内执行任意命令（**逃生口**，需 admin 二次确认）。

设计意图：白名单覆盖不到的边缘场景留一个口子，但每次都强制走 admin 审批 + 全量审计落库。
"""

from __future__ import annotations

import shlex

MANIFEST = {
    "code": "host_run_command",
    "name": "宿主机任意命令（admin）",
    "description": (
        "在指定节点的宿主机 namespace 中执行任意命令——**这是平台唯一的'逃生口'**，"
        "白名单 skill 覆盖不到的临时排障才用。"
        "默认通过 nsenter 进入 PID/MNT/NET/UTS/IPC namespace，等价于在宿主机上跑命令。"
        "**强制 admin 二次确认**，所有调用全审计；不要用本 skill 替代正常诊断 skill。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": False,
    "requires_admin_approval": True,
    "visibility": "admin",
    "confirmation_ttl_seconds": 600,
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令，例如 ``systemctl status docker``",
            },
            "namespaces": {
                "type": "string",
                "default": "muinp",
                "description": "进入哪些 namespace；m=mnt u=uts i=ipc n=net p=pid，默认 muinp 全进",
            },
            "connection_id": {"type": "string"},
        },
        "required": ["node", "command"],
    },
}


def run(
    ctx,
    *,
    node: str,
    command: str,
    namespaces: str = "muinp",
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    inner = ["sh", "-c", command]
    ns_tuple = tuple(c for c in namespaces if c in {"m", "u", "i", "n", "p", "U", "C"})
    result = client.nsenter_on_node(node, inner, namespaces=ns_tuple or ("m", "u", "i", "n", "p"))
    return {
        "node": node,
        "command": command,
        "namespaces": namespaces,
        "ok": result.ok,
        "stdout": result.stdout[-16000:],
        "stderr": result.stderr[-4000:],
        "returncode": result.returncode,
    }
