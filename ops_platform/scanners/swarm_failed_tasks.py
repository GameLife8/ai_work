"""扫 ``docker service ps`` 失败任务列表（Error / CurrentState / Node 字段）。"""

from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CONFIG_ERROR, SIG_IMAGE_PULL_FAIL, SIG_NO_SPACE,
    SIG_OOM_KILL, SIG_PERMISSION_DENIED,
    signal,
)


def scan(failed: list[dict]) -> list[dict]:
    sigs: list[dict] = []
    for task in failed or []:
        err = (task.get("Error") or "").lower()
        state = (task.get("CurrentState") or "").lower()
        node = task.get("Node") or ""
        merged = err + " | " + state

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
            svc = task.get("ServiceName") or (task.get("Name") or "").split(".")[0]
            pivot = {"next_skill": "swarm_query",
                     "next_args": {"category": "service", "verb": "inspect", "name": svc}} if svc else {}
            sigs.append(signal(
                SIG_IMAGE_PULL_FAIL, severity=SEV_CRITICAL,
                evidence=f"任务 {task.get('Name')} 拉镜像失败：{err[:160]}",
                **pivot,
            ))
        elif "permission denied" in merged or "operation not permitted" in merged:
            svc = task.get("ServiceName") or (task.get("Name") or "").split(".")[0]
            pivot = {"next_skill": "swarm_query",
                     "next_args": {"category": "service", "verb": "logs", "name": svc,
                                   "filters": {"grep": "denied"}}} if svc else {}
            sigs.append(signal(
                SIG_PERMISSION_DENIED, severity=SEV_WARNING,
                evidence=f"任务 {task.get('Name')} 报权限错误：{err[:160]}",
                **pivot,
            ))
        elif "exit code 1" in merged or "(1)" in merged:
            svc = task.get("ServiceName") or (task.get("Name") or "").split(".")[0]
            pivot = {"next_skill": "swarm_query",
                     "next_args": {"category": "service", "verb": "logs", "name": svc,
                                   "filters": {"grep": "error"}}} if svc else {}
            sigs.append(signal(
                SIG_CONFIG_ERROR, severity=SEV_WARNING,
                evidence=f"任务 {task.get('Name')} 退出码 1，可能是应用启动错误：{err[:120]}",
                **pivot,
            ))
    return sigs
