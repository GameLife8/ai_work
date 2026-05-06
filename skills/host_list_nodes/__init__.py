"""列出当前 host_agent 集群里所有"已部署 agent"的节点。

调用其它 host_* skill 前，先用本 skill 拿到 ``node`` 列表，再选定某台执行。
"""

from __future__ import annotations

MANIFEST = {
    "code": "host_list_nodes",
    "name": "列出 Agent 节点",
    "description": (
        "返回 host_agent 集群里所有运行中的 agent 节点名。**调用 host_* 系列其它 skill 前先调本 skill** "
        "确认目标节点存在。返回的 node 名可直接当作其它 host_* skill 的 ``node`` 参数。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "connection_id": {"type": "string"},
        },
    },
}


def run(ctx, *, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    return {"kind": getattr(client, "kind", ""), "nodes": client.list_nodes()}
