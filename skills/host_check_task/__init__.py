"""轮询某个异步任务的状态（**走 DB**，不直接连 agent）。

设计意图
========
跟 ``host_run_command_async`` 配对：那个 skill 提交任务拿 ``task_id``，本 skill
负责"再去看一下完了没"。

**读取的是 ``platform_async_task`` 表**，不是 agent 内存：
- agent 重启/任务 GC 不影响读取
- 后台 poller 每 10s 同步一次状态；调用方 get 时如果距上次 poll > 5s 会同步刷一次
- 因此模型轮询永远拿到 ≤ 5s 延迟的结果

read_only=True，不要 admin 审批——查询而已，没有副作用。

模型用法（典型 flow）
----------------------
::

    submit = host_run_command_async(node="bigdata6", command="du -sh /*",
                                     max_runtime_sec=600)
    # submit["task_id"] = "ptk_..."

    res = host_check_task(task_id="ptk_...")
    if res["status"] == "running":
        # 再等会儿；platform 后台 poller 也在持续同步
        ...
    elif res["status"] == "done":
        print(res["stdout"])
    elif res["status"] in ("error", "timeout", "lost"):
        print(res["stderr"])
"""

from __future__ import annotations

MANIFEST = {
    "code": "host_check_task",
    "name": "查询异步任务状态",
    "description": (
        "拉取通过 ``host_run_command_async`` 提交的任务的当前状态 + stdout/stderr。"
        "**读 DB 不直接连 agent**——任务结果永久落库，agent 重启/会话切换不丢。"
        "返回的 ``status`` 取值：``submitting`` / ``running`` / ``done`` / ``error`` / "
        "``timeout`` / ``cancelled`` / ``lost``（agent 端 GC 后查不到 = lost）。"
        "**轮询频率建议**：长命令(>1min)隔 30s 一次；短命令(<1min)隔 5~10s 一次。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            # node / connection_id 已在 DB 行里，不必再传；保留参数是为兼容老 prompt
            "node": {"type": "string", "description": "可选；纯展示用，实际从 DB 读"},
            "connection_id": {"type": "string", "description": "可选；同上"},
        },
        "required": ["task_id"],
    },
}


def run(
    ctx,
    *,
    task_id: str,
    node: str | None = None,
    connection_id: str | None = None,
) -> dict:
    service = getattr(ctx.runtime, "async_task_service", None)
    if service is None:
        return {
            "ok": False,
            "task_id": task_id,
            "error": "AsyncTaskService 未初始化",
        }

    rec = service.get(task_id, refresh=True)
    if rec is None:
        return {
            "ok": False,
            "task_id": task_id,
            "found": False,
            "error": "task_not_found（DB 里没有这个 task_id；可能是手动伪造的，也可能 chat 把 id 记错了）",
        }

    status = rec["status"]
    hint = None
    if status == "submitting":
        hint = "任务正在向 agent 提交；过 1~3s 再查"
    elif status == "running":
        hint = "任务还在跑；过 5~30s 再轮询"
    elif status == "done":
        hint = "任务正常结束，stdout 字段就是结果"
    elif status == "timeout":
        hint = "任务超时被 SIGKILL；重新提交时把 max_runtime_sec 调大"
    elif status == "error":
        hint = "任务非零退出/agent 报错；看 exit_code 和 stderr"
    elif status == "cancelled":
        hint = "任务被取消"
    elif status == "lost":
        hint = (
            "agent 端找不到这个任务且超过宽限期。可能：agent 重启 / 完成 30 分钟后 GC。"
            "DB 里保留了最后一次同步到的 stdout/stderr（可能不完整）；要拿完整结果只能重跑。"
        )

    return {
        "ok": True,
        "task_id": task_id,
        "found": True,
        "status": status,
        "exit_code": rec.get("exit_code"),
        "stdout": (rec.get("stdout") or "")[-16000:],
        "stderr": (rec.get("stderr") or "")[-4000:],
        "node": rec.get("node"),
        "connection_id": rec.get("connection_id"),
        "connection_name": rec.get("connection_name"),
        "command": rec.get("command"),
        "submitted_at": rec.get("submitted_at"),
        "started_at": rec.get("started_at"),
        "ended_at": rec.get("ended_at"),
        "duration_ms": rec.get("duration_ms"),
        "truncated": rec.get("truncated"),
        "max_runtime_sec": rec.get("max_runtime_sec"),
        "last_poll_at": rec.get("last_poll_at"),
        "poll_count": rec.get("poll_count"),
        "_hint": hint,
    }
