"""扫 ``kubectl logs`` 输出（pod 应用日志）。

跟原 k8s_get_pod_logs 内置 _scan_logs 同语义；保留兼容签名 ``scan(text, *, pod, namespace)``。
"""

from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CONFIG_ERROR, SIG_CONNECTION_REFUSED, SIG_DNS_RESOLVE_FAIL,
    SIG_NETWORK_TIMEOUT, SIG_OOM_KILL, SIG_PERMISSION_DENIED,
    signal,
)


def scan(text: str, *, pod: str, namespace: str | None) -> list[dict]:
    if not text:
        return []
    sigs: list[dict] = []
    low = text.lower()

    if "out of memory" in low or "oomkilled" in low or "killed process" in low:
        sigs.append(signal(
            SIG_OOM_KILL, severity=SEV_CRITICAL,
            evidence=f"Pod {pod} 日志中检测到 OOM 关键词（'out of memory' / 'OOMKilled' / 'killed process'）",
            next_skill="zabbix_get_host_overview",
            context={"namespace": namespace, "pod": pod},
        ))

    if "connection refused" in low:
        sigs.append(signal(
            SIG_CONNECTION_REFUSED, severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志报 'connection refused'（依赖服务未监听或被防火墙阻断）",
            next_skill="host_run_command",
            next_args={"command": "ss -ltnup"},
            context={"namespace": namespace, "pod": pod},
        ))
    if "i/o timeout" in low or "context deadline exceeded" in low or "request timeout" in low:
        sigs.append(signal(
            SIG_NETWORK_TIMEOUT, severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志报网络超时（i/o timeout / context deadline exceeded）",
            context={"namespace": namespace, "pod": pod},
        ))

    if any(k in low for k in (
        "no such host", "getaddrinfo", "name resolution",
        "unknownhostexception", "temporary failure in name resolution",
    )):
        sigs.append(signal(
            SIG_DNS_RESOLVE_FAIL, severity=SEV_CRITICAL,
            evidence=(
                f"Pod {pod} 日志报 DNS 解析失败——可能是 CoreDNS 故障 / 网络策略阻断 / "
                "容器 resolv.conf 错"
            ),
            next_skill="kube_query",
            next_args={"verb": "describe", "resource": "pod", "name": pod,
                       "namespace": namespace} if namespace else {"verb": "describe", "resource": "pod", "name": pod},
            context={"namespace": namespace, "pod": pod,
                     "hint": "再用 host_run_command 跑 nsenter 进容器 netns 看 DNS(套路见 container_netns_diag 剧本)"},
        ))

    if "permission denied" in low or "operation not permitted" in low:
        sigs.append(signal(
            SIG_PERMISSION_DENIED, severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志报权限错误：检查 PSP/SCC、挂载点权限、SA token",
            context={"namespace": namespace, "pod": pod},
        ))
    if any(k in low for k in (
        "no such file or directory", "config file not found",
        "invalid configuration", "could not parse config",
    )):
        sigs.append(signal(
            SIG_CONFIG_ERROR, severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志疑似配置错误（找不到文件 / 配置解析失败）",
            context={"namespace": namespace, "pod": pod},
        ))
    return sigs
