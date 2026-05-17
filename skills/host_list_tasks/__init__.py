"""列出异步任务（**走 DB**，看全集群所有任务）。

典型用法
========
- 用户问 "你刚才让我跑的 du 跑完没？" —— 模型忘了 task_id，可以 ``host_list_tasks``
  扫一遍 command 字段
- 模型 self-recovery：上次对话 ``host_run_command_async`` 提交后断了，重新对话
  时调本 skill 拿 ``status=running`` 的任务接着等
- 排查后台任务积压

跟旧版本不同：**直接读 DB**，不再要求传 node，可以看跨节点跨连接的所有任务。
保留 ``node`` 字段做筛选用。
"""

from __future__ import annotations

MANIFEST = {
    "code": "host_list_tasks",
    "name": "列异步任务（走 DB）",
    "description": (
        "列出平台 DB 里的异步任务（含 running/done/error/timeout/cancelled/lost）。"
        "默认按 submitted_at 倒序，最新的在前面。可按节点 / 状态 / session 过滤。"
        "**注意**：本 skill 读 DB 不连 agent；如要拿 agent 内存 GC 后丢的任务原始结果，"
        "需要重跑命令。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "可选；按 node 筛选"},
            "status": {
                "type": "string",
                "description": "可选；按状态筛选：running/done/error/timeout/cancelled/lost/submitting",
            },
            "session_id": {"type": "string", "description": "可选；只看本会话的任务"},
            "submitted_by": {"type": "string", "description": "可选；按提交用户筛选"},
            "limit": {
                "type": "integer", "default": 50, "minimum": 1, "maximum": 500,
                "description": "返回条数上限",
            },
            "connection_id": {"type": "string"},
        },
    },
}


def run(
    ctx,
    *,
    node: str | None = None,
    status: str | None = None,
    session_id: str | None = None,
    submitted_by: str | None = None,
    limit: int = 50,
    connection_id: str | None = None,
) -> dict:
    service = getattr(ctx.runtime, "async_task_service", None)
    if service is None:
        return {"ok": False, "error": "AsyncTaskService 未初始化", "tasks": []}

    # connection_id 是 host_agent 维度的筛选；如果没传就不限制
    items = service.list(
        connection_id=connection_id,
        node=node,
        status=status,
        session_id=session_id,
        submitted_by=submitted_by,
        limit=int(limit),
    )

    # 截断 stdout/stderr —— 列表场景下不需要全文，给个尾巴预览
    out = []
    for t in items:
        out.append({
            "task_id":         t.get("task_id"),
            "status":          t.get("status"),
            "node":            t.get("node"),
            "connection_id":   t.get("connection_id"),
            "connection_name": t.get("connection_name"),
            "command":         (t.get("command") or "")[:200],
            "submitted_by":    t.get("submitted_by"),
            "session_id":      t.get("session_id"),
            "submitted_at":    t.get("submitted_at"),
            "started_at":      t.get("started_at"),
            "ended_at":        t.get("ended_at"),
            "duration_ms":     t.get("duration_ms"),
            "exit_code":       t.get("exit_code"),
            "stdout_preview":  (t.get("stdout") or "")[-1500:],
            "stderr_preview":  (t.get("stderr") or "")[-800:],
            "truncated":       t.get("truncated"),
            "poll_count":      t.get("poll_count"),
            "last_poll_at":    t.get("last_poll_at"),
            "last_poll_error": t.get("last_poll_error"),
        })

    return {
        "ok": True,
        "filters": {
            "node": node, "status": status, "session_id": session_id,
            "submitted_by": submitted_by, "connection_id": connection_id,
        },
        "count": len(out),
        "tasks": out,
    }
