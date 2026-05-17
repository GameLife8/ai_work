"""扫 ``kubectl describe pod`` 输出。

识别：OOMKilled / Evicted (DiskPressure / MemoryPressure) / FailedScheduling /
ImagePullBackOff / CrashLoopBackOff / 探针失败。
"""

from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CRASH_LOOP, SIG_EVICTED, SIG_FAILED_SCHEDULING,
    SIG_IMAGE_PULL_FAIL, SIG_OOM_KILL, SIG_PROBE_FAIL,
    signal,
)


def _extract_node(text: str) -> str | None:
    """从 describe 输出抓 ``Node:  hostname/ip``。"""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Node:") or s.startswith("Node "):
            tail = s.split(":", 1)[-1].strip()
            return tail.split("/")[0].strip() or None
    return None


def scan(text: str, *, name: str, namespace: str | None) -> list[dict]:
    sigs: list[dict] = []
    low = (text or "").lower()
    node = _extract_node(text or "")

    def base_args() -> dict:
        a = {"name": name}
        if namespace:
            a["namespace"] = namespace
        return a

    if "oomkilled" in low:
        sigs.append(signal(
            SIG_OOM_KILL, severity=SEV_CRITICAL,
            evidence=f"Pod {name} Events 显示 OOMKilled" + (f"（Node={node}）" if node else ""),
            next_skill="zabbix_get_host_overview" if node else "k8s_get_pod_logs",
            next_args=({"host_query": node} if node else {**base_args(), "previous": True}),
            context={"node": node},
        ))
    if "evicted" in low and ("diskpressure" in low or "disk-pressure" in low):
        sigs.append(signal(
            SIG_EVICTED, severity=SEV_CRITICAL,
            evidence=f"Pod {name} 因 DiskPressure 被驱逐" + (f"（Node={node}）" if node else ""),
            next_skill="zabbix_get_host_storage_overview" if node else None,
            next_args=({"host_query": node} if node else None),
            context={"node": node, "reason": "DiskPressure"},
        ))
    elif "evicted" in low and ("memorypressure" in low or "memory-pressure" in low):
        sigs.append(signal(
            SIG_EVICTED, severity=SEV_CRITICAL,
            evidence=f"Pod {name} 因 MemoryPressure 被驱逐" + (f"（Node={node}）" if node else ""),
            next_skill="zabbix_get_host_overview" if node else None,
            next_args=({"host_query": node} if node else None),
            context={"node": node, "reason": "MemoryPressure"},
        ))
    if "failedscheduling" in low or "failed scheduling" in low:
        sigs.append(signal(
            SIG_FAILED_SCHEDULING, severity=SEV_WARNING,
            evidence=f"Pod {name} 调度失败：节点资源/污点不满足",
            next_skill="kube_query",
            next_args={"verb": "get", "resource": "pods",
                       "namespace": namespace} if namespace else {"verb": "get", "resource": "pods"},
        ))
    if "imagepullbackoff" in low or "errimagepull" in low or "errimageneverpull" in low:
        sigs.append(signal(
            SIG_IMAGE_PULL_FAIL, severity=SEV_CRITICAL,
            evidence=f"Pod {name} 拉镜像失败（ImagePullBackOff/ErrImagePull）",
            next_skill=None,
        ))
    if "crashloopbackoff" in low:
        sigs.append(signal(
            SIG_CRASH_LOOP, severity=SEV_CRITICAL,
            evidence=f"Pod {name} 进入 CrashLoopBackOff",
            next_skill="k8s_get_pod_logs",
            next_args={**base_args(), "previous": True},
        ))
    if "liveness probe failed" in low or "readiness probe failed" in low or "startup probe failed" in low:
        sigs.append(signal(
            SIG_PROBE_FAIL, severity=SEV_WARNING,
            evidence=f"Pod {name} 探针失败",
            next_skill="k8s_get_pod_logs",
            next_args=base_args(),
        ))
    return sigs
