from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL,
    SEV_WARNING,
    SIG_CONFIG_ERROR,
    SIG_CONNECTION_REFUSED,
    SIG_NETWORK_TIMEOUT,
    SIG_OOM_KILL,
    SIG_PERMISSION_DENIED,
    attach,
    signal,
)

MANIFEST = {
    "code": "k8s_get_pod_logs",
    "name": "拉取 Pod 日志",
    "description": (
        "拉取 pod 应用层日志（``kubectl logs --tail=N``）。"
        "**关键技巧**："
        "  - CrashLoopBackOff 时**必须**带 ``previous=True``——否则只能看到刚启动还没崩的日志；"
        "  - 多容器 pod 必须用 ``container`` 参数指定，否则可能拉到不相关 sidecar 的日志；"
        "  - 怀疑是某个时间窗口的问题用 ``since=5m`` / ``since=1h``；"
        "  - 默认 tail=200 一般够；调到 1000+ 谨慎，模型上下文会被刷掉。"
        "**返回的 ``_signals`` 会扫日志关键词自动 pivot**：OOMKilled / 端口拒绝 / 超时 / 权限错误时建议下一步。"
        "如果日志一切正常但 pod 仍异常，说明问题在系统层（OOM/scheduling/资源），回去调 k8s_describe_pod。"
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
            "container": {"type": "string"},
            # 之前 schema 里没写 default 导致模型可能传 null；放在这里跟 description 一致。
            "tail": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
            "since": {"type": "string", "description": "如 5m / 1h / 2025-01-01T00:00:00Z"},
            "previous": {"type": "boolean", "default": False},
            "connection_id": {"type": "string"},
        },
        "required": ["name"],
    },
}


# 日志关键词 → (signal_type, severity, next_skill, next_args_builder, evidence_tpl)
# 注意：扫日志只看 last N lines（默认 tail=200）；模型决策不应依赖久远的日志。
def _scan_logs(text: str, *, pod: str, namespace: str | None) -> list[dict]:
    """从 pod 日志文本里识别已知失败模式，发结构化 signals。

    设计原则：
    - 一条日志可能触发多个 signal（OOM + permission denied 同时存在很常见），不强制单选。
    - ``next_skill`` 字段必须填能直接接力的 skill；不确定就只发证据不发 next。
    """
    if not text:
        return []
    lower = text.lower()
    sigs: list[dict] = []

    # OOM——优先级最高，因为它跨层（应用感知 + 主机内存压力都可能是因）
    if "out of memory" in lower or "oomkilled" in lower or "killed process" in lower:
        sigs.append(signal(
            SIG_OOM_KILL,
            severity=SEV_CRITICAL,
            evidence=f"Pod {pod} 日志中检测到 OOM 关键词（'out of memory' / 'OOMKilled' / 'killed process'）",
            # 拉主机概览看是不是宿主机内存压力（vs 单 pod limits 太低）
            next_skill="zabbix_get_host_overview",
            # 没有 pod.spec.nodeName 信息，让模型自己补——这里只给 namespace/pod 上下文
            context={"namespace": namespace, "pod": pod},
        ))

    # 端口拒绝 / 网络超时——多半是依赖服务 down 或 iptables 阻断
    if "connection refused" in lower:
        sigs.append(signal(
            SIG_CONNECTION_REFUSED,
            severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志报 'connection refused'（依赖服务未监听或被防火墙阻断）",
            next_skill="host_socket_overview",
            context={"namespace": namespace, "pod": pod},
        ))
    if "i/o timeout" in lower or "context deadline exceeded" in lower or "request timeout" in lower:
        sigs.append(signal(
            SIG_NETWORK_TIMEOUT,
            severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志报网络超时（i/o timeout / context deadline exceeded）",
            context={"namespace": namespace, "pod": pod},
        ))

    # 权限 / 配置错——应用层根因，不是平台层
    if "permission denied" in lower or "operation not permitted" in lower:
        sigs.append(signal(
            SIG_PERMISSION_DENIED,
            severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志报权限错误：检查 PSP/SCC、挂载点权限、SA token",
            context={"namespace": namespace, "pod": pod},
        ))
    if any(k in lower for k in (
        "no such file or directory",
        "config file not found",
        "invalid configuration",
        "could not parse config",
    )):
        sigs.append(signal(
            SIG_CONFIG_ERROR,
            severity=SEV_WARNING,
            evidence=f"Pod {pod} 日志疑似配置错误（找不到文件 / 配置解析失败）",
            context={"namespace": namespace, "pod": pod},
        ))
    return sigs


def run(
    ctx,
    *,
    name: str,
    namespace: str | None = None,
    container: str | None = None,
    tail: int | None = None,
    since: str | None = None,
    previous: bool = False,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("k8s", connection_id)
    result = client.get_pod_logs(
        name,
        namespace=namespace,
        container=container,
        tail=tail,
        since=since,
        previous=previous,
    )
    # 不同 k8s client 实现可能把日志放在 stdout / logs / text 字段——都试一遍
    log_text = ""
    if isinstance(result, dict):
        log_text = (
            result.get("logs")
            or result.get("stdout")
            or result.get("text")
            or ""
        )
    sigs = _scan_logs(log_text, pod=name, namespace=namespace)
    return attach(result, sigs) if isinstance(result, dict) else result
