"""在指定节点上执行**任意命令**——平台唯一的命令执行口。

设计准则（大道至简）
====================
**代码不拼接、不限制、不封装命令** —— 模型给什么 shell 命令就执行什么
（``sh -c <命令>``）。agent 的 ``allowed.yml`` + **每次执行弹确认（当前会话用户点
允许/拒绝）** 是仅有的两道边界。给模型最大自由度。

命令在节点的**宿主机 namespace**（``nsenter -t 1``）里跑。要看某个**容器内部**的
视角（DNS / 连通性 / 进程），自己在命令里 ``nsenter`` 进那个容器的 namespace ——
套路调 ``platform_get_runbooks(name='container_netns_diag')`` 学。

三种模式（同一个口）
====================
1. **同步**（默认）：``node`` + ``command`` → 在宿主机跑，**60s 超时**，直接返回结果。
2. **异步长命令**：再传 ``max_runtime_sec`` → 命令可能跑很久（du 全盘 / find / 大窗口
   journalctl / tcpdump 几分钟），平台改走 agent 的 ``/v1/exec_async``，**立即返回
   ``task_id`` 不阻塞**这一轮对话。
3. **查异步结果**：``task_id`` 单传 → 查那个任务现在跑完没 / 结果。**这是只读,免确认**
   （靠 manifest 的 ``read_only_params``）。
"""

from __future__ import annotations

_DONE_STATUSES = {"done", "error", "timeout", "cancelled", "lost"}


MANIFEST = {
    "code": "host_run_command",
    "name": "节点任意命令",
    "description": (
        "在指定节点上执行**任意 shell 命令**——**平台唯一的命令执行口**,主机排障、"
        "看容器、Swarm/K8s 写操作(docker/kubectl)、临时取证,全走这一个。"
        "**代码不限制命令内容**,模型给什么跑什么(原样 ``sh -c``),"
        "**每次执行都会弹确认(当前会话用户点允许/拒绝)**,全量审计兜底。"
        "默认在**宿主机 namespace**(nsenter -t 1)里**同步**跑(60s 超时);要从**某容器网络视角**"
        "查 DNS/连通性,在命令里自己 nsenter 进容器(套路见 ``platform_get_runbooks(name='container_netns_diag')``)。"
        "**长命令**(du 全盘 / find / 大窗口 journalctl / tcpdump 抓几分钟)传 ``max_runtime_sec`` → 改走**异步**,"
        "立即返回 ``task_id`` 不阻塞;之后调 ``host_run_command(task_id=...)`` 查结果(**查结果免确认**)。"
        "传 ``node`` 即可——平台按节点名自动路由到它所属集群。"
        "**只读查 K8s/Swarm 请用 kube_query / swarm_query**(更结构化、省 token);这个口留给主机命令、写操作。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": False,
    # 只传 task_id = 查异步任务结果,是只读 → 该次调用免确认(见 invoker._is_read_only_invocation)。
    "read_only_params": ["task_id"],
    "requires_admin_approval": False,
    "visibility": "admin",
    "confirmation_ttl_seconds": 600,
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {
                "type": "string",
                "description": "目标节点名(或 IP)。平台按节点名自动路由到所属集群。运行命令时必填。",
            },
            "command": {
                "type": "string",
                "description": (
                    "要执行的 shell 命令,**原样喂给 ``sh -c``**,可用管道/重定向/任意工具。运行命令时必填。"
                    "例:``docker ps -a | grep resource`` / ``nslookup oss.chinasws.com`` / "
                    "``nsenter -t <PID> -n nslookup a.com``"
                ),
            },
            "max_runtime_sec": {
                "type": "integer",
                "description": (
                    "可选。命令预计 **>60s**(du 全盘 / find / 大窗口 journalctl / tcpdump 抓几分钟)就传,"
                    "平台改走**异步**:立即返回 task_id 不阻塞,之后用 task_id 轮询。不传 = 同步跑(60s 超时)。"
                ),
            },
            "task_id": {
                "type": "string",
                "description": "可选。**只传它**(不传 command)= 查之前那个异步任务现在跑完没 / 结果。免确认。",
            },
            "connection_id": {
                "type": "string",
                "description": "可选,显式指定 host_agent 连接;不传则按 node 自动路由。",
            },
        },
    },
}


def _poll(ctx, task_id: str) -> dict:
    """查异步任务结果(只读路径)。"""
    service = getattr(ctx.runtime, "async_task_service", None)
    if service is None:
        return {"ok": False, "error": "异步任务服务未初始化"}
    rec = service.get(str(task_id))
    if not rec:
        return {"ok": False, "task_id": task_id,
                "error": f"任务 {task_id} 不存在(可能已过期被回收)"}
    status = rec.get("status")
    return {
        "task_id": task_id,
        "status": status,
        "done": status in _DONE_STATUSES,
        "node": rec.get("node"),
        "command": rec.get("command"),
        "exit_code": rec.get("exit_code"),
        "stdout": (rec.get("stdout") or "")[-16000:],
        "stderr": (rec.get("stderr") or "")[-4000:],
        "duration_ms": rec.get("duration_ms"),
    }


def _submit_async(ctx, *, node: str, command: str, connection_id, max_runtime_sec: int) -> dict:
    """提交异步长任务,立即返回 task_id。"""
    service = getattr(ctx.runtime, "async_task_service", None)
    if service is None:
        return {"ok": False, "error": "异步任务服务未初始化,无法跑长命令"}
    conn_id = ctx.resolve_connection_id("host_agent", connection_id, node=node)
    if not conn_id:
        return {"ok": False, "error": "未找到该 node 所属的 host_agent 连接"}
    rec = service.submit(
        connection_id=conn_id,
        node=node,
        command=command,
        argv=["sh", "-c", command],
        nsenter="muinp",
        max_runtime_sec=int(max_runtime_sec),
        submitted_by=(ctx.user or {}).get("username"),
        session_id=ctx.session_id,
    )
    return {
        "async": True,
        "task_id": rec.get("task_id"),
        "status": rec.get("status"),
        "node": node,
        "command": command,
        "hint": f"已异步提交,稍后用 host_run_command(task_id='{rec.get('task_id')}') 查结果",
    }


def run(
    ctx,
    *,
    node: str | None = None,
    command: str | None = None,
    max_runtime_sec: int | None = None,
    task_id: str | None = None,
    connection_id: str | None = None,
) -> dict:
    # 1) 轮询模式:只查异步任务结果(只读,免确认)
    if task_id:
        return _poll(ctx, task_id)

    # 运行模式:node + command 必填
    if not node or not command:
        return {"ok": False,
                "error": "运行命令需要 node + command(或只传 task_id 查异步任务结果)"}

    # 2) 异步模式:命令可能跑很久 → 提交后立即返回 task_id,不阻塞
    if max_runtime_sec:
        return _submit_async(ctx, node=node, command=command,
                             connection_id=connection_id, max_runtime_sec=max_runtime_sec)

    # 3) 同步模式:在宿主机全 namespace 跑(nsenter -t 1),60s 超时
    # 准则:不解析、不改写命令——原样塞进 sh -c。进容器由模型在命令里自己 nsenter。
    client = ctx.connection_for("host_agent", connection_id, node=node)
    result = client.nsenter_on_node(
        node, ["sh", "-c", command], namespaces=("m", "u", "i", "n", "p"))
    return {
        "node": node,
        "command": command,
        "ok": result.ok,
        "stdout": result.stdout[-16000:],
        "stderr": result.stderr[-4000:],
        "returncode": result.returncode,
    }
