"""Skill: 查询主机某项指标在最近 N 小时内的峰值。

回答 "最近一次 CPU 最高值"、"最近一次内存最高值" 类问题。

设计要点
--------
- 规则在 ``services.metric_analytics.find_peak``——provider-agnostic。
- 本 skill 只负责"参数 -> 调 connection -> 调规则 -> 转 signal"，不碰具体监控产品。
- 当前 ``required_connection_type='zabbix'``，是因为目前只接了 Zabbix；
  接入 Prometheus / Datadog 时只要再写一个实现 ``MetricProvider`` 的 client +
  driver，本 skill 改一个常量即可（或加 ``provider_type`` 参数让用户选）。
"""

from __future__ import annotations

import logging

from ops_platform.signals import (
    SEV_CRITICAL,
    SEV_WARNING,
    SIG_HIGH_CPU,
    SIG_HIGH_MEM,
    attach,
    signal,
)
from services.metric_analytics import (
    DEFAULT_PEAK_CONTEXT_SECONDS,
    DEFAULT_PEAK_LOOKBACK_SECONDS,
    find_peak,
)
from services.metric_provider import KNOWN_METRICS


logger = logging.getLogger(__name__)


# 触发哪个 signal——根据用户问的指标决定（CPU 高 -> SIG_HIGH_CPU 等）。
_METRIC_TO_SIGNAL = {
    "cpu.utilization":    SIG_HIGH_CPU,
    "memory.utilization": SIG_HIGH_MEM,
}


MANIFEST = {
    "code": "metric_query_peak",
    "name": "查询指标最近峰值",
    "description": (
        "查询指定主机某项指标在最近 N 小时内出现的**最大值**（或最小值），并返回峰值时刻"
        "前后 ±2.5 分钟的原始采样上下文 + 全窗口 avg / max / min / p95 / last 统计。"
        "**典型场景**："
        "(1) 用户问「最近一次 CPU 最高是多少」「过去 24 小时内存最高峰」「这台机昨天 load 最高的时候是几点」——直接调本 skill；"
        "(2) 排障时想确认尖峰是否还在持续——拿 peak.timestamp 跟当前时间比较；"
        "(3) 跟用户给的「故障时刻」对照，看峰值时间是否吻合。"
        "支持的 metric：cpu.utilization / memory.utilization / system.load.avg1。"
        "返回 peak 字段是单个时刻的精确值；context_samples 提供尖峰前后 5 分钟"
        "原始点，用来判断尖峰是瞬时毛刺还是持续高负载。"
    ),
    "category": "monitoring",
    "required_connection_type": "zabbix",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "host_query": {
                "type": "string",
                "description": "主机名 / IP / hostid，三种都接受。",
            },
            "metric": {
                "type": "string",
                "enum": list(KNOWN_METRICS),
                "description": (
                    "逻辑指标名。CPU 用率传 cpu.utilization，内存用率传 memory.utilization，"
                    "系统 load 传 system.load.avg1。"
                ),
            },
            "lookback_hours": {
                "type": "number",
                "default": 24,
                "minimum": 0.083,         # 5 min
                "maximum": 720,           # 30 d
                "description": (
                    "回看多少小时找峰值。默认 24（最近一天）；用户问「最近一周」传 168。"
                    "窗口越大原始点越多，>7d 时 Zabbix history.get 可能截断（见 warning 字段）。"
                ),
            },
            "direction": {
                "type": "string",
                "enum": ["max", "min"],
                "default": "max",
                "description": "max 找最高值（默认）；min 找最低值（少见，比如查内存最低剩余）。",
            },
            "context_window_seconds": {
                "type": "integer",
                "default": DEFAULT_PEAK_CONTEXT_SECONDS,
                "minimum": 60,
                "maximum": 3600,
                "description": "峰值时刻前后各取一半作为上下文采样窗口；默认 ±2.5 分钟。",
            },
            "connection_id": {
                "type": "string",
                "description": "可选。指定使用哪个 connection；不填走当前会话默认。",
            },
        },
        "required": ["host_query", "metric"],
    },
}


def _extract_signals(result: dict) -> list[dict]:
    """峰值过高时挂 signal，让 agent 跨域 pivot（比如 CPU 峰值过高时建议拉
    主机概览看持续负载）。

    设计要点：
    - host_name 为空时**不挂 next_skill**——免得给模型一个 ``{"host_query": ""}`` 的
      无效 next_args，调下游 skill 直接失败。只发证据让模型自己消化。
    - warning 阈值（80~90% CPU）也要给 next_skill：之前缺这条，导致 80~90% 区间
      模型拿到 signal 但没 pivot 提示，跨域诊断卡住。
    """
    peak = result.get("peak") or {}
    metric = result.get("metric")
    value = peak.get("value")
    host_name = (result.get("host") or {}).get("host_name") or ""
    sig_type = _METRIC_TO_SIGNAL.get(metric)

    if not (sig_type and isinstance(value, (int, float))):
        return []

    # host_name 缺失时跳过 next_skill / next_args——不给模型递空指针
    pivot: dict = {}
    if host_name:
        pivot = {
            "next_skill": "zabbix_get_host_overview",
            "next_args": {"host_query": host_name},
        }

    base_ctx = {"peak_time": peak.get("time"), "metric": metric}
    lookback_h = (result.get("lookback_seconds") or 0) // 3600

    if metric == "cpu.utilization":
        if value >= 90:
            return [signal(
                sig_type, severity=SEV_CRITICAL,
                evidence=(
                    f"主机 {host_name or '?'} {lookback_h}h 内 CPU 峰值 "
                    f"{value}%（@ {peak.get('time')}），≥ 90% 严重负载"
                ),
                context=base_ctx,
                **pivot,
            )]
        if value >= 80:
            return [signal(
                sig_type, severity=SEV_WARNING,
                evidence=(
                    f"主机 {host_name or '?'} CPU 峰值 {value}%（@ {peak.get('time')}），"
                    f"接近告警阈值"
                ),
                context=base_ctx,
                **pivot,    # 之前 warning 分支没挂 next_skill，模型拿到只能干瞪眼；现在补上
            )]
    elif metric == "memory.utilization":
        if value >= 90:
            return [signal(
                sig_type, severity=SEV_CRITICAL,
                evidence=(
                    f"主机 {host_name or '?'} 内存峰值 {value}%（@ {peak.get('time')}），"
                    f"≥ 90% 接近 OOM 风险"
                ),
                context=base_ctx,
                **pivot,
            )]
        if value >= 80:
            return [signal(
                sig_type, severity=SEV_WARNING,
                evidence=(
                    f"主机 {host_name or '?'} 内存峰值 {value}%（@ {peak.get('time')}），"
                    f"接近高水位"
                ),
                context=base_ctx,
                **pivot,
            )]
    return []


def run(
    ctx,
    *,
    host_query: str,
    metric: str,
    lookback_hours: float = 24,
    direction: str = "max",
    context_window_seconds: int = DEFAULT_PEAK_CONTEXT_SECONDS,
    connection_id: str | None = None,
) -> dict:
    if metric not in KNOWN_METRICS:
        raise ValueError(
            f"未知 metric: {metric}（支持的：{', '.join(KNOWN_METRICS)}）"
        )

    provider = ctx.connection_for("zabbix", connection_id)
    lookback_seconds = max(1, int(float(lookback_hours) * 3600))
    if lookback_seconds > 30 * 24 * 3600:
        # 跟 manifest schema 兜底一致
        lookback_seconds = 30 * 24 * 3600
        logger.warning("lookback_hours 超过 720h，已 cap 到 30d。")

    result = find_peak(
        provider,
        host_query,
        metric,
        lookback_seconds=lookback_seconds,
        direction=direction,
        context_window_seconds=int(context_window_seconds),
    )
    return attach(result, _extract_signals(result))
