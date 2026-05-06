from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING, SIG_REPLICAS_INSUFFICIENT,
    attach, signal,
)
from skills.swarm_get_failed_tasks import _extract_signals as extract_failed_task_signals

MANIFEST = {
    "code": "swarm_check_service_health",
    "name": "Swarm 服务健康检查",
    "description": (
        "汇总 Swarm 服务的副本数、更新状态、重启策略和最近失败任务。"
        "**适合作为容器排障的入口**——大多数「服务起不来 / 不停重启 / 发布失败」问题都先调这个。"
        "返回内容里的 failed_tasks 字段如出现 OOMKilled / exit 137 / no space / Evicted，"
        "应立刻 (a) 调 swarm_get_failed_tasks 拿到 Node 名，(b) 用 Node 名当 host_query 调 "
        "zabbix_get_host_overview / zabbix_get_host_storage_overview，定位是不是宿主机问题。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["service_name"],
    },
}


def _parse_replicas(status: dict) -> tuple[int, int]:
    """从 'service ls' 的 Replicas 字段（如 '0/3'）解析出 ready/desired。"""
    raw = (status or {}).get("Replicas") or ""
    if "/" in raw:
        try:
            ready, desired = raw.split("/", 1)
            return int(ready), int(desired)
        except ValueError:
            pass
    return 0, 0


def run(ctx, *, service_name: str, connection_id: str | None = None) -> dict:
    result = ctx.connection_for("swarm", connection_id).check_service_health(service_name)
    failed = result.get("failed_tasks") or []
    for t in failed:
        t.setdefault("ServiceName", service_name)

    sigs = extract_failed_task_signals(failed)

    ready, desired = _parse_replicas(result.get("status") or {})
    if desired and ready < desired:
        severity = SEV_CRITICAL if ready == 0 else SEV_WARNING
        sigs.append(signal(
            SIG_REPLICAS_INSUFFICIENT, severity=severity,
            evidence=f"服务 {service_name} 副本不足：{ready}/{desired}",
            next_skill="swarm_get_failed_tasks",
            next_args={"service_name": service_name},
        ))

    return attach(result, sigs)
