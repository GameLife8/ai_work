from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CRASH_LOOP, SIG_IMAGE_PULL_FAIL,
    attach, signal,
)

MANIFEST = {
    "code": "k8s_list_pods",
    "name": "列出 K8s Pod",
    "description": (
        "列出 namespace 下的 pod，含 phase / ready / restarts / waiting_reasons / node。"
        "**用法**：作为 K8s 排障的入口先调一次 → 找出 Not Ready / 高 restart 的目标 → "
        "对那个 pod 再调 k8s_describe_pod 看 Events、k8s_get_pod_logs 看应用层。"
        "返回字段 ``waiting_reasons`` 里如果有 CrashLoopBackOff / ImagePullBackOff / "
        "ErrImageNeverPull / CreateContainerConfigError，直接定位重点。"
        "返回字段 ``node`` 可作为 zabbix host_query 反查宿主机。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "label_selector": {"type": "string", "description": "可选；如 app=foo,env=prod"},
            "connection_id": {"type": "string"},
        },
    },
}


def _extract_pod_signals(pods: list[dict], namespace: str | None) -> list[dict]:
    sigs: list[dict] = []
    seen_types: set[tuple] = set()
    for p in pods:
        reasons = [r for r in (p.get("waiting_reasons") or []) if r]
        if not reasons:
            continue
        name = p.get("name") or ""
        ns = p.get("namespace") or namespace
        for reason in reasons:
            r = reason.lower()
            if "crashloop" in r:
                key = ("crash", name, ns)
                if key in seen_types: continue
                seen_types.add(key)
                sigs.append(signal(
                    SIG_CRASH_LOOP, severity=SEV_CRITICAL,
                    evidence=f"Pod {name} 处于 CrashLoopBackOff（重启 {p.get('restarts')} 次）",
                    next_skill="k8s_describe_pod",
                    next_args={"name": name, "namespace": ns} if ns else {"name": name},
                ))
            elif "imagepull" in r or "errimage" in r:
                key = ("img", name, ns)
                if key in seen_types: continue
                seen_types.add(key)
                sigs.append(signal(
                    SIG_IMAGE_PULL_FAIL, severity=SEV_CRITICAL,
                    evidence=f"Pod {name} 拉镜像失败（{reason}）",
                    next_skill="k8s_describe_pod",
                    next_args={"name": name, "namespace": ns} if ns else {"name": name},
                ))
    return sigs


def run(ctx, *, namespace: str | None = None, label_selector: str | None = None,
        connection_id: str | None = None) -> dict:
    client = ctx.connection_for("k8s", connection_id)
    result = client.list_pods(namespace=namespace, label_selector=label_selector)
    sigs = _extract_pod_signals(result.get("pods") or [], namespace)
    return attach(result, sigs)
