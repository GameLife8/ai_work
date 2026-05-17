"""异步执行宿主机命令——给长命令兜底（**admin 二次确认**）。

为啥要这个 skill
================
同步 ``host_run_command`` 走 ``/v1/exec``，agent 端硬上限 300s；超过 timeout 后
agent SIGKILL 子进程，调用方也早就 HTTP 超时了。但实际排障常有这种命令：

- ``du -sh /*`` —— 节点磁盘各顶级目录占用，几十 GB 的根分区可能跑 5~15min
- ``find / -mtime -1 -size +100M`` —— 找一天内变大的文件
- ``tcpdump -G 60 -W 1 -w /tmp/cap.pcap any`` —— 抓 60s 包
- ``journalctl --since '2 hours ago' | grep ERROR`` —— 海量日志过滤

这类长命令同步等就是死路。解法：

1. 平台 ``AsyncTaskService.submit()`` → **先写 DB**（status=submitting，task_id
   由平台生成 PK）→ 调 agent → 回写 DB（status=running）
2. agent 后台跑命令；DB 是 source of truth
3. 模型/用户隔几秒调 ``host_check_task(task_id=...)`` 拿状态；后台 poller 也会
   定期同步

**所有任务全部持久化到 ``platform_async_task`` 表**——agent 重启、chat 会话
中断、30min agent GC 都不影响平台拿结果。
"""

from __future__ import annotations

MANIFEST = {
    "code": "host_run_command_async",
    "name": "宿主机长命令（异步, admin）",
    "description": (
        "**异步**在指定节点宿主机 namespace 跑长命令。立即返回 ``task_id``，命令在 agent "
        "后台跑，最长 30 分钟。"
        "**任务全程持久化到平台 DB**：agent 重启、会话中断都不会丢结果。"
        "**用法**："
        "  1. 调本 skill → 拿 ``task_id``；"
        "  2. 隔 5~30s 调 ``host_check_task(task_id=...)`` 轮询；"
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
    service = getattr(ctx.runtime, "async_task_service", None)
    if service is None:
        return {
            "ok": False,
            "error": "AsyncTaskService 未初始化；请检查 runtime 启动日志",
        }

    # 解析最终 connection_id 入库审计
    resolved_conn_id = ctx.resolve_connection_id("host_agent", connection_id)
    if not resolved_conn_id:
        return {"ok": False, "error": "未找到 host_agent 类型的 connection"}

    # 跟同步 host_run_command 一样把 command 包成 sh -c —— agent 白名单已放行 sh
    argv = ["sh", "-c", command]
    ns_clean = "".join(c for c in namespaces if c in {"m", "u", "i", "n", "p", "U", "C"}) or "muinp"

    user = (ctx.user or {}).get("username")
    rec = service.submit(
        connection_id=resolved_conn_id,
        node=node,
        command=command,
        argv=argv,
        nsenter=ns_clean,
        max_runtime_sec=max_runtime_sec,
        max_output_bytes=max_output_bytes,
        submitted_by=user or "system",
        session_id=ctx.session_id,
    )

    is_err = rec["status"] == "error" or not rec.get("status")
    return {
        "ok": not is_err,
        "task_id": rec["task_id"],
        "status": rec["status"],
        "node": node,
        "command": command,
        "namespaces": ns_clean,
        "connection_id": resolved_conn_id,
        "submitted_at": rec.get("submitted_at"),
        "started_at": rec.get("started_at"),
        "max_runtime_sec": rec.get("max_runtime_sec"),
        "error": rec.get("last_poll_error") if is_err else None,
        "_hint": (
            f"任务已落库（DB 是 source of truth）。隔 5~30s 调 "
            f"host_check_task(task_id='{rec['task_id']}') 拿结果；"
            f"agent / 会话异常都不会丢数据。"
        ) if not is_err else None,
    }
