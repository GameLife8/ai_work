"""结构化 Signals —— 让 skill 之间的跨域 pivot 从概率事件变成确定性。

设计动机
--------
之前模型靠从 stdout 文本里 grep "OOMKilled" / "no space" / "ImagePullBackOff"
来决定下一步调啥 skill。这是软约束，prompt 提了几次仍有概率漏。

引入 ``signals`` 后：
- 每个 skill 在返回数据时附带一个 ``_signals: [Signal, ...]`` 列表
- agent 在 tool 循环里读到 signals → 自动拼接一段 system 提示
  "上一步发现 oom_kill 信号（host=W1），建议下一步调
  zabbix_get_host_overview(host_query='W1')"
- 模型看到这段 hint 时调用对应 skill 的概率 ↑↑↑

约定
----
``Signal`` 是 **dict 不是 dataclass**——保持 JSON 友好，方便往返、写入 trace、
喂给模型。字段约定：

    {
      "type":        str,         必填；语义代码，如 "oom_kill" / "disk_pressure"
      "severity":    str,         "info" | "warning" | "critical"，默认 "warning"
      "evidence":    str,         自然语言证据（写入 audit + 喂模型）
      "next_skill":  str | None,  建议下一步调用的 skill code
      "next_args":   dict | None, 建议下一步参数
      "context":     dict | None, 任意附带（不影响 pivot 行为，只是给后续 skill 用）
    }

注意事项
--------
- 同一个 skill 可以发出多个 signals（一次诊断里可能 oom_kill + disk_pressure 同时存在）
- ``next_skill`` 只是"建议"，最终调用还是模型决定；signals 不强制行动
- agent 会把同一个 (next_skill, next_args) 的 signals 去重，防止 tool 循环
"""

from __future__ import annotations

from typing import Any


# 信号类型常量；新增自由扩展，但这里集中维护让 skill 写起来不会手抖
SIG_OOM_KILL              = "oom_kill"
SIG_DISK_PRESSURE         = "disk_pressure"
SIG_NO_SPACE              = "no_space_left"
SIG_IMAGE_PULL_FAIL       = "image_pull_fail"
SIG_PROBE_FAIL            = "probe_failed"
SIG_NETWORK_TIMEOUT       = "network_timeout"
SIG_CONNECTION_REFUSED    = "connection_refused"
SIG_PORT_NOT_LISTENING    = "port_not_listening"
SIG_PORT_BLOCKED          = "port_blocked_by_iptables"
SIG_CONFIG_ERROR          = "config_error"
SIG_PERMISSION_DENIED     = "permission_denied"
SIG_HIGH_CPU              = "high_cpu"
SIG_HIGH_MEM              = "high_memory"
SIG_HIGH_DISK             = "high_disk"
SIG_HOST_UNREACHABLE      = "host_unreachable"
SIG_AGENT_DOWN            = "zabbix_agent_down"
SIG_REPLICAS_INSUFFICIENT = "replicas_insufficient"
SIG_FAILED_SCHEDULING     = "failed_scheduling"
SIG_EVICTED               = "pod_evicted"
SIG_CRASH_LOOP            = "crash_loop_backoff"
SIG_OOM_LOG               = "oom_in_kernel_log"
SIG_CONNTRACK_FULL        = "conntrack_table_full"
SIG_DISK_IO_ERROR         = "disk_io_error"
SIG_DNS_RESOLVE_FAIL      = "dns_resolve_fail"


# 严重程度
SEV_INFO     = "info"
SEV_WARNING  = "warning"
SEV_CRITICAL = "critical"


def signal(
    type_: str,
    *,
    evidence: str,
    severity: str = SEV_WARNING,
    next_skill: str | None = None,
    next_args: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个 signal dict 的便捷工厂。"""
    return {
        "type": type_,
        "severity": severity,
        "evidence": evidence,
        "next_skill": next_skill,
        "next_args": dict(next_args) if next_args else None,
        "context": dict(context) if context else None,
    }


def attach(result: dict[str, Any], signals: list[dict[str, Any]]) -> dict[str, Any]:
    """往 skill 返回 dict 上挂一组 signals（保持原 result 其它字段）。"""
    if not signals:
        return result
    existing = result.get("_signals") or []
    result["_signals"] = list(existing) + list(signals)
    return result


def collect(envelope_or_result: dict[str, Any]) -> list[dict[str, Any]]:
    """从 invoker envelope 或裸 result dict 里拿 signals 列表。"""
    if not isinstance(envelope_or_result, dict):
        return []
    if "_signals" in envelope_or_result:
        return list(envelope_or_result.get("_signals") or [])
    inner = envelope_or_result.get("result")
    if isinstance(inner, dict) and "_signals" in inner:
        return list(inner.get("_signals") or [])
    return []


def dedup_key(sig: dict[str, Any]) -> tuple:
    """用于去重：同一个 (type, next_skill, next_args 的关键字段) 被多次报时只算一次。"""
    args = sig.get("next_args") or {}
    arg_kvs = tuple(sorted((k, str(v)) for k, v in args.items()))
    return (sig.get("type"), sig.get("next_skill"), arg_kvs)


def render_hint(signals: list[dict[str, Any]]) -> str:
    """把一组 signals 渲染成喂给模型的中文 system 提示。"""
    if not signals:
        return ""
    lines = ["⚡ 上一步 skill 检测到结构化信号，建议如下跨域取证："]
    for i, s in enumerate(signals, 1):
        sev = s.get("severity", SEV_WARNING)
        sev_icon = {"info": "·", "warning": "⚠", "critical": "🚨"}.get(sev, "·")
        line = f"  {i}. {sev_icon} [{s.get('type')}] {s.get('evidence', '')}"
        if s.get("next_skill"):
            args = s.get("next_args") or {}
            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
            line += f"\n     → 建议调用：{s['next_skill']}({args_str})"
        lines.append(line)
    lines.append("（如已经掌握足够证据可直接给最终报告，无需重复调用同一参数的 skill）")
    return "\n".join(lines)
