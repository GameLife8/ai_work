"""在指定节点的指定接口抓包 N 秒（写操作，需用户确认）。"""

from __future__ import annotations

MANIFEST = {
    "code": "host_capture_packets",
    "name": "宿主机抓包",
    "description": (
        "在指定节点的指定接口上 ``tcpdump`` 抓包 N 秒，过滤表达式可选。"
        "返回包数 + 摘要文本（带 ``-w -`` 二进制流量比较大，先只返回文本头）。"
        "属于写操作（会消耗节点 CPU + 网络），需用户确认；"
        "建议 duration ≤ 30s，filter 写得越精确越好（如 ``host 1.2.3.4 and port 443``）。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": False,
    "requires_admin_approval": False,
    "visibility": "all",
    "confirmation_ttl_seconds": 180,
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "interface": {"type": "string", "default": "any"},
            "filter": {"type": "string", "description": "tcpdump BPF 表达式，如 'tcp port 443 and host 1.2.3.4'"},
            "duration": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
            "max_packets": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 200},
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


def run(
    ctx,
    *,
    node: str,
    interface: str = "any",
    filter: str | None = None,
    duration: int = 10,
    max_packets: int = 200,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    cmd = [
        "timeout", str(int(duration)),
        "tcpdump", "-i", interface,
        "-n", "-vv",
        "-c", str(int(max_packets)),
    ]
    if filter:
        cmd.append(filter)
    result = client.nsenter_on_node(node, cmd)
    # tcpdump 退出码：被 timeout 杀掉返回 124 也是正常
    return {
        "node": node,
        "interface": interface,
        "filter": filter,
        "duration": int(duration),
        "max_packets": int(max_packets),
        "ok": result.ok or result.returncode == 124,
        "summary": result.stdout[-8000:],   # 截断防止吃 token
        "stderr": result.stderr[-2000:],
    }
