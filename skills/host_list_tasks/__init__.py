"""列某节点上所有异步任务（运维盲查 / LLM 自我恢复用）。

read_only，不需要 admin 审批。

典型用法
========
- 用户问 "你刚才让我跑的 du 跑完没？" —— 模型忘了 task_id，可以 ``host_list_tasks``
  扫一遍 command 字段
- 排查 agent 内存占用——同时跑 16 个任务封顶 (ASYNC_MAX_CONCURRENT_TASKS)，
  本 skill 看下到底有多少在跑
- 模型 self-recovery：上次对话 ``host_run_command_async`` 提交后断了，重新对话
  时调本 skill 找 ``running`` 任务接着等

返回结构
========
::

    {
      "node": "bigdata6",
      "running": 2,
      "limits": {"max_concurrent": 16, "max_runtime_sec": 1800, ...},
      "tasks": [
        {"task_id": "...", "status": "running", "command": [...], ...},
        ...
      ]
    }
"""

from __future__ import annotations

MANIFEST = {
    "code": "host_list_tasks",
    "name": "列宿主机上所有异步任务",
    "description": (
        "列出指定节点 agent 内存里当前所有异步任务（含 running/done/error/timeout/cancelled）。"
        "用来：找忘记 task_id 的任务、查 agent 并发占用、对话恢复时接着轮询。"
        "默认按启动时间倒序，最新的在前面。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "status_filter": {
                "type": "string",
                "description": "可选；只看某状态：running/done/error/timeout/cancelled",
            },
            "connection_id": {"type": "string"},
        },
        "required": ["node"],
    },
}


def run(
    ctx,
    *,
    node: str,
    status_filter: str | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    payload = client.task_list_on_node(node)

    if "error" in payload and "tasks" not in payload:
        return {
            "node": node,
            "ok": False,
            "error": payload.get("error"),
            "tasks": [],
            "running": 0,
        }

    tasks = payload.get("tasks") or []
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]

    # 截断 stdout/stderr —— 列表场景下不需要全文，给个尾巴预览
    for t in tasks:
        if t.get("stdout"):
            t["stdout"] = t["stdout"][-2000:]
        if t.get("stderr"):
            t["stderr"] = t["stderr"][-1000:]

    return {
        "node": node,
        "ok": True,
        "running": payload.get("running"),
        "limits": payload.get("limits"),
        "status_filter": status_filter,
        "count": len(tasks),
        "tasks": tasks,
    }
