"""扫 ``kubectl get pods`` slim 列表（每个 pod summary dict）。

输入是 ``[{name, namespace, phase, ready, restarts, waiting_reasons, node, ...}]``。
"""

from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL,
    SIG_CRASH_LOOP, SIG_IMAGE_PULL_FAIL,
    signal,
)


def scan(pods: list[dict], *, namespace: str | None = None) -> list[dict]:
    sigs: list[dict] = []
    seen: set[tuple] = set()
    for p in pods or []:
        reasons = [r for r in (p.get("waiting_reasons") or []) if r]
        if not reasons:
            continue
        name = p.get("name") or ""
        ns = p.get("namespace") or namespace
        for reason in reasons:
            r = reason.lower()
            if "crashloop" in r:
                key = ("crash", name, ns)
                if key in seen:
                    continue
                seen.add(key)
                sigs.append(signal(
                    SIG_CRASH_LOOP, severity=SEV_CRITICAL,
                    evidence=f"Pod {name} 处于 CrashLoopBackOff（重启 {p.get('restarts')} 次）",
                    next_skill="kube_query",
                    next_args={"verb": "describe", "resource": "pod", "name": name,
                               "namespace": ns} if ns else {"verb": "describe", "resource": "pod", "name": name},
                ))
            elif "imagepull" in r or "errimage" in r:
                key = ("img", name, ns)
                if key in seen:
                    continue
                seen.add(key)
                sigs.append(signal(
                    SIG_IMAGE_PULL_FAIL, severity=SEV_CRITICAL,
                    evidence=f"Pod {name} 拉镜像失败（{reason}）",
                    next_skill="kube_query",
                    next_args={"verb": "describe", "resource": "pod", "name": name,
                               "namespace": ns} if ns else {"verb": "describe", "resource": "pod", "name": name},
                ))
    return sigs
