"""轮询某个 ``host_run_command_async`` 任务的状态。

设计意图
========
跟 ``host_run_command_async`` 配对：那个 skill 提交任务拿 ``task_id``，本 skill
负责"再去看一下完了没"。

read_only=True，不要 admin 审批——查询自己提交过的任务而已，没有副作用。

模型用法（典型 flow）
----------------------
::

    submit = host_run_command_async(node="bigdata6", command="du -sh /*",
                                     max_runtime_sec=600)
    # submit["task_id"] = "tk_a1b2c3d4..."

    # 等一会再查
    res = host_check_task(node="bigdata6", task_id="tk_a1b2c3d4...")
    if res["status"] == "running":
        # 再等会儿
        ...
    elif res["status"] == "done":
        print(res["stdout"])
    elif res["status"] in ("error", "timeout"):
        print(res["stderr"])

返回字段全部从 agent ``/v1/task/<id>`` 透传过来。
"""

from __future__ import annotations

MANIFEST = {
    "code": "host_check_task",
    "name": "查询异步任务状态",
    "description": (
        "拉取通过 ``host_run_command_async`` 提交的任务的当前状态 + stdout/stderr。"
        "返回的 ``status`` 取值：``running`` / ``done`` / ``error`` / ``timeout`` / "
        "``cancelled``。"
        "**轮询频率建议**：长命令(>1min)隔 30s 一次；短命令(<1min)隔 5~10s 一次。"
        "**注意**：任务完成后 agent 保留 30 分钟，过期就 404。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "task_id": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["node", "task_id"],
    },
}


def run(
    ctx,
    *,
    node: str,
    task_id: str,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    payload = client.task_get_on_node(node, task_id)

    # agent 端 404 → payload 是 {"error": "task_not_found", ...}
    if "error" in payload and not payload.get("task_id"):
        return {
            "node": node,
            "task_id": task_id,
            "ok": False,
            "found": payload.get("error") != "task_not_found",
            "error": payload.get("error"),
            "status": None,
        }

    status = payload.get("status")
    # 给模型一个明确的语义提示，避免它把 running 当 done 用
    hint = None
    if status == "running":
        hint = "任务还在跑，过 5~30s 再轮询一次"
    elif status == "done":
        hint = "任务正常结束，stdout 字段就是结果"
    elif status == "timeout":
        hint = (
            "任务超时被 SIGKILL；如果还要继续，重新提交时把 max_runtime_sec 调大"
        )
    elif status == "error":
        hint = "任务非零退出，看 exit_code 和 stderr 排错"
    elif status == "cancelled":
        hint = "任务被取消"

    return {
        "node": node,
        "task_id": task_id,
        "ok": True,
        "found": True,
        "status": status,
        "exit_code": payload.get("exit_code"),
        "stdout": (payload.get("stdout") or "")[-16000:],
        "stderr": (payload.get("stderr") or "")[-4000:],
        "started_at": payload.get("started_at"),
        "ended_at": payload.get("ended_at"),
        "duration_ms": payload.get("duration_ms"),
        "truncated": payload.get("truncated"),
        "max_runtime_sec": payload.get("max_runtime_sec"),
        "command": payload.get("command"),
        "_hint": hint,
    }
