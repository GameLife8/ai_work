from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CONFIG_ERROR, SIG_IMAGE_PULL_FAIL, SIG_NO_SPACE,
    SIG_OOM_KILL, SIG_PERMISSION_DENIED,
    attach, signal,
)

MANIFEST = {
    "code": "swarm_get_failed_tasks",
    "name": "查看失败任务",
    "description": (
        "查看服务最近失败任务列表，含 exit code、error message、Node、时间戳。"
        "**这是把容器问题向主机层切换的关键 pivot 点**——任务行里的 Node 字段，"
        "可以直接当作 zabbix_get_host_overview 的 host_query 用。"
        "看到 137=OOM / 139=segfault / 'no space left' 时，几乎一定要追主机维度。"
        "看到 'context deadline exceeded' / 'connection refused' 时，可能是网络/依赖问题。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "limit": {"type": "integer"},
            "connection_id": {"type": "string"},
        },
        "required": ["service_name"],
    },
}


def _extract_signals(failed: list[dict]) -> list[dict]:
    """从 swarm task error 文本中识别已知模式，发出结构化信号。"""
    sigs: list[dict] = []
    for task in failed:
        err = (task.get("Error") or "").lower()
        state = (task.get("CurrentState") or "").lower()
        node = task.get("Node") or ""
        merged = err + " | " + state

        # 137 = SIGKILL, 多半是 OOMKill
        if "exit code 137" in merged or "(137)" in merged or "oomkilled" in merged:
            sigs.append(signal(
                SIG_OOM_KILL, severity=SEV_CRITICAL,
                evidence=f"任务 {task.get('Name')} 在节点 {node} 退出码 137（多半 OOMKilled）：{err[:120]}",
                next_skill="zabbix_get_host_overview",
                next_args={"host_query": node} if node else None,
                context={"node": node, "exit_clue": "137"},
            ))
        elif "no space left" in merged or "disk full" in merged or "write failed" in merged:
            sigs.append(signal(
                SIG_NO_SPACE, severity=SEV_CRITICAL,
                evidence=f"任务 {task.get('Name')} 在节点 {node} 报磁盘满：{err[:120]}",
                next_skill="zabbix_get_host_storage_overview",
                next_args={"host_query": node} if node else None,
                context={"node": node},
            ))
        elif "pull access denied" in merged or "manifest unknown" in merged or "image pull" in merged:
            sigs.append(signal(
                SIG_IMAGE_PULL_FAIL, severity=SEV_CRITICAL,
                evidence=f"任务 {task.get('Name')} 拉镜像失败：{err[:160]}",
                next_skill="swarm_get_service_detail",
                next_args={"service_name": task.get("ServiceName") or task.get("Name", "").split(".")[0]},
            ))
        elif "permission denied" in merged or "operation not permitted" in merged:
            sigs.append(signal(
                SIG_PERMISSION_DENIED, severity=SEV_WARNING,
                evidence=f"任务 {task.get('Name')} 报权限错误：{err[:160]}",
                next_skill="swarm_get_service_logs_filter",
                next_args={"service_name": task.get("ServiceName") or "", "keyword": "denied"},
            ))
        elif "exit code 1" in merged or "(1)" in merged:
            # 通用退出码 1，多半是配置/启动脚本错误
            sigs.append(signal(
                SIG_CONFIG_ERROR, severity=SEV_WARNING,
                evidence=f"任务 {task.get('Name')} 退出码 1，可能是应用启动错误：{err[:120]}",
                next_skill="swarm_get_service_logs_filter",
                next_args={"service_name": task.get("ServiceName") or "", "keyword": "error"},
            ))
    return sigs


def run(ctx, *, service_name: str, limit: int = 10, connection_id: str | None = None) -> dict:
    result = ctx.connection_for("swarm", connection_id).get_failed_tasks(service_name, limit)
    failed = result.get("failed_tasks") or []
    # task 没带 ServiceName 时补一下，方便信号 pivot 用
    for t in failed:
        t.setdefault("ServiceName", service_name)
    return attach(result, _extract_signals(failed))
