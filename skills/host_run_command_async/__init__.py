"""异步执行宿主机命令（**admin 二次确认**）——给长命令兜底。

为啥要这个 skill
================
同步 ``host_run_command`` 走 ``/v1/exec``，agent 端硬上限 300s；超过 timeout 后
agent 会 SIGKILL 子进程，调用方也早就 HTTP 超时了。但实际排障常有这种命令：

- ``du -sh /*`` —— 节点磁盘各顶级目录占用，几十 GB 的根分区可能跑 5~15min
- ``find / -mtime -1 -size +100M`` —— 找一天内变大的文件
- ``tcpdump -G 60 -W 1 -w /tmp/cap.pcap any`` —— 抓 60s 包
- ``journalctl --since '2 hours ago' | grep ERROR`` —— 海量日志过滤

这类长命令同步等就是死路。解法：

1. 用本 skill 提交 → agent 返回 ``task_id`` 立即结束（200ms 量级）
2. agent 端在后台继续跑（asyncio fire-and-forget），完成后把 stdout/stderr 存
   内存里 30 分钟可查
3. 模型隔几秒调 ``host_check_task(task_id=...)`` 拿状态——running 就再等，done
   就读结果

跟同步 ``host_run_command`` 一样**强制 admin 二次确认**，全审计。
"""

from __future__ import annotations

MANIFEST = {
    "code": "host_run_command_async",
    "name": "宿主机长命令（异步, admin）",
    "description": (
        "**异步**在指定节点宿主机 namespace 跑长命令。立即返回 ``task_id``，命令在 agent "
        "后台跑，最长 30 分钟。"
        "**用法**："
        "  1. 调本 skill → 拿 ``task_id``；"
        "  2. 隔 5~30s 调 ``host_check_task(node=..., task_id=...)`` 轮询；"
        "  3. status=done/error/timeout 时 ``stdout``/``stderr`` 字段就有结果了。"
        "**适用场景**：``du -sh /*`` / ``find / ...`` / ``tcpdump -G N -W 1 -w ...`` / "
        "``journalctl --since ...`` 等估计 > 30s 的命令。"
        "短命令（<30s）直接用 ``host_run_command`` 同步路径，省一次轮询。"
        "**仅 transport=http 的 host_agent 支持异步**——transport=exec 会直接报错。"
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
                "description": "要执行的 shell 命令，例如 ``du -sh /*``",
            },
            "namespaces": {
                "type": "string",
                "default": "muinp",
                "description": "进入哪些 namespace；m=mnt u=uts i=ipc n=net p=pid，默认 muinp 全进",
            },
            "max_runtime_sec": {
                "type": "integer",
                "default": 300,
                "minimum": 5,
                "maximum": 1800,
                "description": "最大跑多久；超时 agent 端 SIGKILL（默认 300s，硬上限 30min）",
            },
            "max_output_bytes": {
                "type": "integer",
                "description": "stdout/stderr 截断阈值；默认跟 agent 同步路径相同（1MB）",
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
    max_runtime_sec: int = 300,
    max_output_bytes: int | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)

    # 跟同步 host_run_command 一样把 command 包成 sh -c —— agent 白名单已放行 sh
    inner = ["sh", "-c", command]
    ns_tuple = tuple(c for c in namespaces if c in {"m", "u", "i", "n", "p", "U", "C"})
    ns_tuple = ns_tuple or ("m", "u", "i", "n", "p")

    payload = client.exec_async_on_node(
        node, inner,
        namespaces=ns_tuple,
        max_runtime_sec=max_runtime_sec,
        max_output_bytes=max_output_bytes,
    )
    # 兜底：agent 端返回 error 时把 ok=False 暴露给模型
    is_err = "error" in payload and not payload.get("task_id")
    return {
        "node": node,
        "command": command,
        "namespaces": namespaces,
        "max_runtime_sec": max_runtime_sec,
        "ok": not is_err,
        "task_id": payload.get("task_id"),
        "status": payload.get("status"),
        "started_at": payload.get("started_at"),
        "poll_endpoint": payload.get("poll_endpoint"),
        "error": payload.get("error"),
        "_hint": (
            "稍等 5~30s 再调 host_check_task(node='{node}', task_id='{tid}') 拿结果"
            .format(node=node, tid=payload.get("task_id"))
        ) if not is_err else None,
    }
