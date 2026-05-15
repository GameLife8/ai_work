from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_CONNECTION_REFUSED, SIG_DNS_RESOLVE_FAIL, SIG_NETWORK_TIMEOUT,
    SIG_NO_SPACE, SIG_OOM_KILL, SIG_PERMISSION_DENIED,
    attach, signal,
)

MANIFEST = {
    "code": "swarm_get_service_logs_filter",
    "name": "按关键字过滤 Swarm 日志",
    "description": (
        "按关键词过滤服务日志（应用层）。**通常一次 keyword 抓不全，要换多个关键词追**："
        "先 'error' / 'exception'，再 'timeout' / 'OOM' / 'denied' / 'refused' / 'panic'。"
        "tail 默认 100 一般够；只有要看历史时再加大到 500-1000。"
        "如果日志里完全看不出问题，但服务确实异常，去查 swarm_get_failed_tasks 的系统层退出码。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "service_name": {"type": "string"},
            "keyword": {"type": "string"},
            "tail": {"type": "integer"},
            "since": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["service_name", "keyword"],
    },
}


def _scan_log_signals(service_name: str, log_text: str) -> list[dict]:
    """扫描日志文本里的常见错误模式，发出结构化信号。

    每种 type 只发一次（用 set 去重），避免日志里 timeout 出现 50 次刷屏。
    """
    sigs: list[dict] = []
    seen: set[str] = set()
    low = (log_text or "").lower()

    if ("connection refused" in low or "ecconnrefused" in low) and SIG_CONNECTION_REFUSED not in seen:
        seen.add(SIG_CONNECTION_REFUSED)
        # 没 node 信息时**不**带 next_skill —— 避免给模型递空 args 让下游 skill 直接
        # 报"node 必填"。模型看到 evidence + signal type 自己会知道下一步该追主机层。
        sigs.append(signal(
            SIG_CONNECTION_REFUSED, severity=SEV_CRITICAL,
            evidence=(
                f"日志中出现 'connection refused'（服务 {service_name}），可能下游不可达 / "
                "端口未监听。建议先 swarm_get_failed_tasks 拿 Node，再 host_socket_overview 看监听。"
            ),
            next_skill="swarm_get_failed_tasks",
            next_args={"service_name": service_name},
        ))

    if ("context deadline exceeded" in low or "i/o timeout" in low or "request timeout" in low) and SIG_NETWORK_TIMEOUT not in seen:
        seen.add(SIG_NETWORK_TIMEOUT)
        sigs.append(signal(
            SIG_NETWORK_TIMEOUT, severity=SEV_WARNING,
            evidence=(
                f"日志中出现网络 timeout（服务 {service_name}）。先拿 Node 再查路由 / "
                "conntrack / DNS。"
            ),
            next_skill="swarm_get_failed_tasks",
            next_args={"service_name": service_name},
        ))

    # DNS 解析失败 —— Go/Java/Python 各自的 DNS 错误关键词
    if any(p in low for p in (
        "no such host", "getaddrinfo", "name resolution",
        "unknownhostexception", "temporary failure in name resolution",
    )) and SIG_DNS_RESOLVE_FAIL not in seen:
        seen.add(SIG_DNS_RESOLVE_FAIL)
        sigs.append(signal(
            SIG_DNS_RESOLVE_FAIL, severity=SEV_CRITICAL,
            evidence=(
                f"日志中出现 DNS 解析失败（服务 {service_name}）—— 关键词命中"
                " ``no such host`` / ``getaddrinfo`` 等。先看节点 /etc/resolv.conf 跟"
                " coredns 状态。"
            ),
            next_skill="swarm_get_failed_tasks",
            next_args={"service_name": service_name},
            context={"hint": "下一步可 host_inspect_container_netns 看容器内 DNS 配置 + host_route_overview 看节点路由"},
        ))

    if ("out of memory" in low or "oomkill" in low or "java.lang.outofmemoryerror" in low) and SIG_OOM_KILL not in seen:
        seen.add(SIG_OOM_KILL)
        sigs.append(signal(
            SIG_OOM_KILL, severity=SEV_CRITICAL,
            evidence=f"日志中出现 OOM 字样（服务 {service_name}）",
            next_skill="swarm_get_failed_tasks",
            next_args={"service_name": service_name},
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
            next_skill="swarm_get_service_detail",
            next_args={"service_name": service_name},
        ))

    return sigs


def run(
    ctx,
    *,
    service_name: str,
    keyword: str,
    tail: int | None = None,
    since: str | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("swarm", connection_id)
    result = client.get_service_logs_filter(service_name, keyword, tail, since)
    sigs = _scan_log_signals(service_name, result.get("matched_logs") or "")
    return attach(result, sigs)
