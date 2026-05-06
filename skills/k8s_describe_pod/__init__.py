from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CRASH_LOOP, SIG_EVICTED, SIG_FAILED_SCHEDULING,
    SIG_IMAGE_PULL_FAIL, SIG_OOM_KILL, SIG_PROBE_FAIL,
    attach, signal,
)

MANIFEST = {
    "code": "k8s_describe_pod",
    "name": "Describe K8s Pod",
    "description": (
        "对指定 pod 执行 ``kubectl describe pod``，返回事件、状态、镜像、卷、limits/requests 等完整信息。"
        "**重点看 Events 段**——常见信号到下一步映射："
        "  - OOMKilled / Last State Reason=OOMKilled → limits 不够，或宿主机 MEM 紧张："
        "    用 spec.nodeName 当 host_query 调 zabbix_get_host_overview；"
        "  - Evicted (reason: DiskPressure) → 该 Node 磁盘满："
        "    用 nodeName 调 zabbix_get_host_storage_overview；"
        "  - FailedScheduling → 节点资源/污点问题，调 k8s_list_pods 看整体占用；"
        "  - ImagePullBackOff / ErrImagePull → 镜像引用或 imagePullSecrets 配错；"
        "  - 探针 Liveness/Readiness 失败 → 调 k8s_get_pod_logs 看应用层。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "namespace": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["name"],
    },
}


def _extract_node_from_describe(text: str) -> str | None:
    """从 describe 输出里抓 ``Node:  hostname/ip``。"""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Node:") or s.startswith("Node "):
            tail = s.split(":", 1)[-1].strip()
            return tail.split("/")[0].strip() or None
    return None


def _scan_describe_signals(pod_name: str, namespace: str | None, text: str) -> list[dict]:
    sigs: list[dict] = []
    low = (text or "").lower()
    node = _extract_node_from_describe(text or "")

    def base_args() -> dict:
        a = {"name": pod_name}
        if namespace:
            a["namespace"] = namespace
        return a

    if "oomkilled" in low:
        sigs.append(signal(
            SIG_OOM_KILL, severity=SEV_CRITICAL,
            evidence=f"Pod {pod_name} Events 显示 OOMKilled" + (f"（Node={node}）" if node else ""),
            next_skill="zabbix_get_host_overview" if node else "k8s_get_pod_logs",
            next_args=({"host_query": node} if node else {**base_args(), "previous": True}),
            context={"node": node},
        ))
    if "evicted" in low and ("diskpressure" in low or "disk-pressure" in low):
        sigs.append(signal(
            SIG_EVICTED, severity=SEV_CRITICAL,
            evidence=f"Pod {pod_name} 因 DiskPressure 被驱逐" + (f"（Node={node}）" if node else ""),
            next_skill="zabbix_get_host_storage_overview" if node else None,
            next_args=({"host_query": node} if node else None),
            context={"node": node, "reason": "DiskPressure"},
        ))
    elif "evicted" in low and ("memorypressure" in low or "memory-pressure" in low):
        sigs.append(signal(
            SIG_EVICTED, severity=SEV_CRITICAL,
            evidence=f"Pod {pod_name} 因 MemoryPressure 被驱逐" + (f"（Node={node}）" if node else ""),
            next_skill="zabbix_get_host_overview" if node else None,
            next_args=({"host_query": node} if node else None),
            context={"node": node, "reason": "MemoryPressure"},
        ))
    if "failedscheduling" in low or "failed scheduling" in low:
        sigs.append(signal(
            SIG_FAILED_SCHEDULING, severity=SEV_WARNING,
            evidence=f"Pod {pod_name} 调度失败：节点资源/污点不满足",
            next_skill="k8s_list_pods",
            next_args={"namespace": namespace} if namespace else {},
        ))
    if "imagepullbackoff" in low or "errimagepull" in low or "errimageneverpull" in low:
        sigs.append(signal(
            SIG_IMAGE_PULL_FAIL, severity=SEV_CRITICAL,
            evidence=f"Pod {pod_name} 拉镜像失败（ImagePullBackOff/ErrImagePull）",
            next_skill=None,  # 镜像问题模型直接给建议即可
        ))
    if "crashloopbackoff" in low:
        sigs.append(signal(
            SIG_CRASH_LOOP, severity=SEV_CRITICAL,
            evidence=f"Pod {pod_name} 进入 CrashLoopBackOff",
            next_skill="k8s_get_pod_logs",
            next_args={**base_args(), "previous": True},
        ))
    if "liveness probe failed" in low or "readiness probe failed" in low or "startup probe failed" in low:
        sigs.append(signal(
            SIG_PROBE_FAIL, severity=SEV_WARNING,
            evidence=f"Pod {pod_name} 探针失败",
            next_skill="k8s_get_pod_logs",
            next_args=base_args(),
        ))
    return sigs


def run(ctx, *, name: str, namespace: str | None = None, connection_id: str | None = None) -> dict:
    result = ctx.connection_for("k8s", connection_id).describe_pod(name, namespace=namespace)
    sigs = _scan_describe_signals(name, namespace, result.get("describe") or "")
    return attach(result, sigs)
