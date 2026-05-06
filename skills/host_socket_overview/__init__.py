from __future__ import annotations

from ops_platform.signals import (
    SEV_WARNING, SIG_PORT_NOT_LISTENING,
    attach, signal,
)

MANIFEST = {
    "code": "host_socket_overview",
    "name": "宿主机端口监听一览",
    "description": (
        "在指定节点的 **宿主机网络 namespace** 中执行 ``ss -tunlp``，"
        "返回全部 TCP/UDP 监听端口、连接状态、对应进程。"
        "用于排查「为什么某个端口连不上 / 谁在监听 X 端口 / 服务实际是不是在 listen」。"
        "可选 ``filter`` 参数做行级 grep（如 'LISTEN' / ':80' / 'docker'）。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "filter": {"type": "string", "description": "可选；对输出做 grep"},
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


def run(ctx, *, node: str, filter: str | None = None, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    inner = ["ss", "-tunlp"]
    result = client.nsenter_on_node(node, inner)
    out = result.stdout
    matched_lines = out.splitlines()
    if filter:
        matched_lines = [line for line in matched_lines if filter.lower() in line.lower()]
        out = "\n".join(matched_lines)

    payload = {**result.to_dict(), "stdout": out, "filter": filter}

    sigs: list[dict] = []
    # 当用户/模型用 filter 查特定端口（含 ':' 形态）但没匹配时，发出"端口未监听"信号
    if filter and ":" in filter and not any("LISTEN" in ln for ln in matched_lines):
        sigs.append(signal(
            SIG_PORT_NOT_LISTENING, severity=SEV_WARNING,
            evidence=f"节点 {node} 上没有进程监听 {filter}（ss 输出中未发现 LISTEN）",
            next_skill="host_iptables_dump",
            next_args={"node": node},
        ))
    return attach(payload, sigs)
