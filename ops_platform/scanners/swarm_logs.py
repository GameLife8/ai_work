"""扫 ``docker service logs`` 应用日志（含过滤后的 matched_logs）。

跟原 swarm_get_service_logs_filter 同语义。
"""

from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CONNECTION_REFUSED, SIG_DNS_RESOLVE_FAIL, SIG_NETWORK_TIMEOUT,
    SIG_NO_SPACE, SIG_OOM_KILL, SIG_PERMISSION_DENIED,
    signal,
)


def scan(service_name: str, log_text: str) -> list[dict]:
    sigs: list[dict] = []
    seen: set[str] = set()
    low = (log_text or "").lower()

    if ("connection refused" in low or "ecconnrefused" in low) and SIG_CONNECTION_REFUSED not in seen:
        seen.add(SIG_CONNECTION_REFUSED)
        sigs.append(signal(
            SIG_CONNECTION_REFUSED, severity=SEV_CRITICAL,
            evidence=(
                f"日志中出现 'connection refused'（服务 {service_name}）。"
                "建议先 swarm_query(category=service, verb=ps) 拿 Node，再 host_query(command='ss -ltnup') 看监听。"
            ),
            next_skill="swarm_query",
            next_args={"category": "service", "verb": "ps", "name": service_name,
                       "filters": {"desired-state": "failed"}},
        ))

    if ("context deadline exceeded" in low or "i/o timeout" in low or "request timeout" in low) and SIG_NETWORK_TIMEOUT not in seen:
        seen.add(SIG_NETWORK_TIMEOUT)
        sigs.append(signal(
            SIG_NETWORK_TIMEOUT, severity=SEV_WARNING,
            evidence=f"日志中出现网络 timeout（服务 {service_name}）。先拿 Node 再查路由/conntrack/DNS。",
            next_skill="swarm_query",
            next_args={"category": "service", "verb": "ps", "name": service_name},
        ))

    if any(p in low for p in (
        "no such host", "getaddrinfo", "name resolution",
        "unknownhostexception", "temporary failure in name resolution",
    )) and SIG_DNS_RESOLVE_FAIL not in seen:
        seen.add(SIG_DNS_RESOLVE_FAIL)
        sigs.append(signal(
            SIG_DNS_RESOLVE_FAIL, severity=SEV_CRITICAL,
            evidence=(
                f"日志中出现 DNS 解析失败（服务 {service_name}）。"
                "先看节点 /etc/resolv.conf 跟 coredns 状态。"
            ),
            next_skill="swarm_query",
            next_args={"category": "service", "verb": "ps", "name": service_name},
            context={"hint": "下一步可 host_inspect_container_netns 看容器内 DNS 配置"},
        ))

    if ("out of memory" in low or "oomkill" in low or "java.lang.outofmemoryerror" in low) and SIG_OOM_KILL not in seen:
        seen.add(SIG_OOM_KILL)
        sigs.append(signal(
            SIG_OOM_KILL, severity=SEV_CRITICAL,
            evidence=f"日志中出现 OOM 字样（服务 {service_name}）",
            next_skill="swarm_query",
            next_args={"category": "service", "verb": "ps", "name": service_name,
                       "filters": {"desired-state": "failed"}},
        ))

    if ("no space left" in low or "disk full" in low) and SIG_NO_SPACE not in seen:
        seen.add(SIG_NO_SPACE)
        sigs.append(signal(
            SIG_NO_SPACE, severity=SEV_CRITICAL,
            evidence=f"日志报磁盘满（服务 {service_name}）",
            next_skill="zabbix_get_host_storage_overview",
        ))

    if ("permission denied" in low or "access denied" in low) and SIG_PERMISSION_DENIED not in seen:
        seen.add(SIG_PERMISSION_DENIED)
        sigs.append(signal(
            SIG_PERMISSION_DENIED, severity=SEV_WARNING,
            evidence=f"日志报权限错误（服务 {service_name}）",
            next_skill="swarm_query",
            next_args={"category": "service", "verb": "inspect", "name": service_name},
        ))
    return sigs
