"""异步执行宿主机命令 —— 现在是 **hybrid 同步/异步**:命令在 ``wait_seconds`` 内跑完
直接返回完整结果(用户全程无感)。超时才降级异步路径返回 ``task_id``。

为啥要这个 skill
================
同步 ``host_run_command`` 走 ``/v1/exec``,agent 端硬上限 300s;超过 timeout 后
agent SIGKILL 子进程,调用方也早就 HTTP 超时了。但实际排障常有这种命令:

- ``du -xh --max-depth=1 /data`` —— 看一级目录占用,通常 < 30s
- ``du -sh /*`` —— 节点根分区,几十 GB 的可能跑 5~15min
- ``find / -mtime -1 -size +100M`` —— 找一天内变大的文件
- ``tcpdump -G 60 -W 1 -w /tmp/cap.pcap any`` —— 抓 60s 包

如果让用户自己调 ``host_check_task`` 轮询,体验很差。Hybrid 设计:
**``wait_seconds`` 内任务跑完 → 平台直接返回结果(就像同步一样)。超时才落异步**。

执行链路
========
::

    1. invoker._prepare       → needs_confirmation + token(写操作还是要 admin 审批)
    2. user 审批后 invoker.confirm
    3. skill.run() 调用:
         3a. service.submit()    —— 提交到 agent + 写 DB
         3b. wait_seconds 内 polling service.get()
              ├─ 任务到终态(done/error/timeout) → 返回完整结果
              └─ 超过 wait_seconds 仍 running   → 返回 task_id + status=running
                  (用户继续问"看刚才那个任务"时调 host_check_task 即可)

DB 是 source of truth
======================
任务全程持久化到 ``platform_async_task`` 表 —— agent 重启 / chat 会话中断 /
30min agent GC 都不影响平台拿结果。
"""

from __future__ import annotations

import time


# Polling 节奏:前期密集(短任务别等久),后期拉长(长任务避免压力)
_POLL_INTERVALS = [1, 2, 3, 5, 8, 13]   # 总累计 32s(覆盖默认 wait_seconds=30 的典型场景)
_POLL_MAX_INTERVAL = 15                  # 之后每次都等这么久


MANIFEST = {
    "code": "host_run_command_async",
    "name": "宿主机命令(hybrid 同步/异步, admin)",
    "description": (
        "**何时用本 skill vs host_query**:\n"
        "  • ``host_query`` 用于**耗时可预测且很短**的命令(``ss -ltnup`` / ``ip route`` / "
        "    ``df -h`` / ``cat /etc/<config>`` 等)——输出小、瞬时返回。\n"
        "  • **耗时跟数据规模相关、不可预测**的(``du`` / ``find`` / ``journalctl`` / "
        "    ``grep -r`` 等)→ **一律用本 skill**——/data 100G 跟 100T 速度差千倍,你猜不准。\n"
        "  • 带 pipe / shell 元字符的命令必须本 skill(host_query 禁止 shell 字符)。\n"
        "\n"
        "**Hybrid 同步/异步**:在 ``wait_seconds``(默认 30s)内任务跑完 → **直接返回**完整 "
        "``stdout``/``stderr``/``exit_code``,你无感同步,直接基于结果写报告——**绝不要让用户去调 "
        "host_check_task**。超过 wait_seconds 还在跑 → 返回 ``task_id`` + ``status=running``,这时"
        "才让用户后续问「跑完了吗」。\n"
        "\n"
        "**wait_seconds 怎么选**(你说不准就估保守一点):\n"
        "  • 不确定 → 默认 30,够覆盖 90% 的轻量 du/find;\n"
        "  • 已知数据规模较大(``du -sh /`` 一个 T 级目录 / ``journalctl --since 1d``)→ 60-120;\n"
        "  • 真长 running(``tcpdump -G 600`` / 大盘 ``find /``)→ 0(立即拿 task_id,不傻等)。\n"
        "\n"
        "**任务全程持久化**,agent 重启 / 会话中断都不丢数据。"
        "**仅 transport=http 的 host_agent 支持**。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": False,
    # ``visibility=admin`` 已经把访问限制在 admin 角色——审批人就是 admin 自己,
    # 不再需要 ``requires_admin_approval=True`` 自己审自己(语义冗余)。当前会话用户点确认即执行。
    "requires_admin_approval": False,
    "visibility": "admin",
    "confirmation_ttl_seconds": 600,
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令,例如 ``du -xh --max-depth=1 /data``",
            },
            "namespaces": {
                "type": "string",
                "default": "muinp",
                "description": "进入哪些 namespace;m=mnt u=uts i=ipc n=net p=pid,默认 muinp",
            },
            "max_runtime_sec": {
                "type": "integer",
                "default": 300,
                "minimum": 5,
                "maximum": 1800,
                "description": "**任务侧**最大跑多久 —— 超时 agent SIGKILL(默认 300s,上限 30min)",
            },
            "wait_seconds": {
                "type": "integer",
                "default": 30,
                "minimum": 0,
                "maximum": 300,
                "description": (
                    "**调用方等多久**才肯接受 task_id 走异步。"
                    "0=纯异步(立即拿 task_id);30=等最多 30 秒,期间命令跑完直接拿完整结果。"
                    "**跟 max_runtime_sec 是两码事**:wait_seconds 是平台等多久,"
                    "max_runtime_sec 是 agent 端任务硬超时。"
                ),
            },
            "max_output_bytes": {
                "type": "integer",
                "description": "stdout/stderr 截断阈值;默认 1MB",
            },
            "connection_id": {"type": "string"},
        },
        "required": ["node", "command"],
    },
}


def _poll_until_terminal(service, task_id: str, wait_seconds: int) -> dict | None:
    """同步轮询 service.get(),最长等 ``wait_seconds`` 秒。

    Returns:
        task 终态 dict —— 完成 / error / timeout / cancelled / lost
        None         —— 等满 wait_seconds 仍 running(调用方应降级为返回 task_id)
    """
    from ops_platform.async_tasks import TERMINAL_STATUSES

    deadline = time.time() + max(0, wait_seconds)
    idx = 0
    while True:
        rec = service.get(task_id, refresh=True)
        if rec is None:
            return None
        if rec["status"] in TERMINAL_STATUSES:
            return rec

        # 算下次睡多久 —— 不超过 deadline
        if idx < len(_POLL_INTERVALS):
            interval = _POLL_INTERVALS[idx]
        else:
            interval = _POLL_MAX_INTERVAL
        idx += 1
        remaining = deadline - time.time()
        if remaining <= 0:
            return None    # 等满了仍在 running
        time.sleep(min(interval, remaining))


def run(
    ctx,
    *,
    node: str,
    command: str,
    namespaces: str = "muinp",
    max_runtime_sec: int = 300,
    wait_seconds: int = 30,
    max_output_bytes: int | None = None,
    connection_id: str | None = None,
) -> dict:
    service = getattr(ctx.runtime, "async_task_service", None)
    if service is None:
        return {
            "ok": False,
            "error": "AsyncTaskService 未初始化;请检查 runtime 启动日志",
        }

    # 解析最终 connection_id 入库审计
    resolved_conn_id = ctx.resolve_connection_id("host_agent", connection_id, node=node)
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

    submit_err = rec["status"] == "error" or not rec.get("status")
    if submit_err:
        return {
            "ok": False,
            "task_id": rec.get("task_id"),
            "status": rec.get("status") or "error",
            "node": node, "command": command,
            "namespaces": ns_clean,
            "connection_id": resolved_conn_id,
            "error": rec.get("last_poll_error") or "submit failed",
        }

    task_id = rec["task_id"]

    # ---- Hybrid 核心:wait_seconds 内 polling,等到终态就直接给完整结果 ---- #
    final_rec: dict | None = None
    if wait_seconds and wait_seconds > 0:
        final_rec = _poll_until_terminal(service, task_id, wait_seconds=wait_seconds)

    if final_rec is not None:
        # ✅ 任务在 wait_seconds 内跑完了 —— 直接返回完整结果(用户无感同步)
        return {
            "ok": final_rec["status"] == "done" and (final_rec.get("exit_code") or 0) == 0,
            "task_id": task_id,
            "status": final_rec["status"],          # done / error / timeout / cancelled
            "exit_code": final_rec.get("exit_code"),
            "stdout": final_rec.get("stdout") or "",
            "stderr": final_rec.get("stderr") or "",
            "duration_ms": final_rec.get("duration_ms"),
            "node": node, "command": command,
            "namespaces": ns_clean,
            "connection_id": resolved_conn_id,
            "submitted_at": final_rec.get("submitted_at"),
            "started_at": final_rec.get("started_at"),
            "ended_at": final_rec.get("ended_at"),
            "wait_mode": "completed_inline",
        }

    # 超过 wait_seconds 仍在 running → 降级走异步路径,返回 task_id
    cur = service.get(task_id, refresh=False) or rec
    return {
        "ok": True,
        "task_id": task_id,
        "status": cur.get("status"),                # 通常是 "running"
        "node": node, "command": command,
        "namespaces": ns_clean,
        "connection_id": resolved_conn_id,
        "submitted_at": cur.get("submitted_at"),
        "started_at": cur.get("started_at"),
        "max_runtime_sec": cur.get("max_runtime_sec"),
        "wait_mode": "deferred_to_async",
        "_hint": (
            f"已在前台等了 {wait_seconds}s 任务仍在跑;切异步路径。"
            f"用户下一轮问「任务跑完了吗」时调 host_check_task(task_id='{task_id}') 拿结果。"
        ),
    }
