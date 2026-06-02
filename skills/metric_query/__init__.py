"""Skill: 主机指标时序查询——peak（找峰值）+ window（取时刻附近采样）二合一。

合并自旧的 ``metric_query_peak`` + ``metric_query_window_around``。两者本质都是
"查主机某指标的时序数据"，只是切片方式不同：

- ``mode=peak``   找最近 N 小时内的**最大/最小值** + 峰值前后上下文 + 全窗口统计
- ``mode=window`` 取**给定时刻 ±N 分钟**的全部原始采样

合并理由
--------
- 两者共享 host_query / metric / connection_id，描述高度重叠，模型经常分不清调哪个
- 典型协作链就是 "先 peak 找到峰值时刻 → 再 window 拉峰值附近细粒度"——
  合成一个 skill 后这条链在同一把口里完成，参数语义也更统一

规则实现仍在 ``services.metric_analytics``（provider-agnostic），本 skill 只做
"参数 → 选 mode → 调规则 → peak 模式额外转 signal"。
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
    find_peak,
    fetch_window,
)
from services.metric_provider import KNOWN_METRICS


logger = logging.getLogger(__name__)


# peak 模式下，CPU/内存峰值过高时挂 signal 让 agent 跨域 pivot。
_METRIC_TO_SIGNAL = {
    "cpu.utilization":    SIG_HIGH_CPU,
    "memory.utilization": SIG_HIGH_MEM,
}


MANIFEST = {
    "code": "metric_query",
    "name": "主机指标时序查询（峰值 / 时刻附近）",
    "description": (
        "**主机指标时序查询的唯一入口**——查 CPU / 内存 / load 的历史曲线。两种 mode：\n"
        "- ``mode=peak``：最近 N 小时内的**峰值**（最大/最小）+ 峰值时刻 ±2.5 分钟上下文 + "
        "全窗口 avg/max/min/p95/last 统计。用户问「最近一次 CPU 最高多少」「过去 24h 内存峰值」"
        "「昨天 load 最高是几点」→ 用这个。\n"
        "- ``mode=window``：**给定时刻 ±N 分钟**的全部原始采样（默认 ±10min）。用户问"
        "「12 点半左右 CPU 多少」「告警时刻前后内存波动」→ 用这个；``center_time`` 不传=当前时刻。\n"
        "**典型协作链**：先 ``mode=peak`` 找到峰值时刻，再 ``mode=window`` 拉那个时刻附近细粒度。\n"
        "支持 metric：cpu.utilization / memory.utilization / system.load.avg1。\n"
        "**注意**：实时单值（不是历史曲线）请走 ``zabbix_get_host_overview``；本 skill 是时序。"
    ),
    "category": "monitoring",
    "required_connection_type": "zabbix",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["peak", "window"],
                "description": "peak=找峰值；window=取某时刻附近采样。必填。",
            },
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
            # ---- peak 模式参数 ----
            "lookback_hours": {
                "type": "number",
                "default": 24,
                "minimum": 0.083,
                "maximum": 720,
                "description": "[peak] 回看多少小时找峰值。默认 24；问「最近一周」传 168。",
            },
            "direction": {
                "type": "string",
                "enum": ["max", "min"],
                "default": "max",
                "description": "[peak] max 找最高（默认）；min 找最低（如查内存最低剩余）。",
            },
            "context_window_seconds": {
                "type": "integer",
                "default": DEFAULT_PEAK_CONTEXT_SECONDS,
                "minimum": 60,
                "maximum": 3600,
                "description": "[peak] 峰值时刻前后各取一半作上下文；默认 ±2.5 分钟。",
            },
            # ---- window 模式参数 ----
            "center_time": {
                "type": "string",
                "description": (
                    "[window] 中心时间。支持 ISO 8601 / 「%Y-%m-%d %H:%M:%S」/ epoch 秒/毫秒；"
                    "无时区按 Asia/Shanghai 解析。不传=当前时间（即「最近 ±N 分钟」）。"
                ),
            },
            "half_width_minutes": {
                "type": "number",
                "default": 10,
                "minimum": 1,
                "maximum": 360,
                "description": "[window] 中心时间前后各取多少分钟。默认 10（±10min，整窗 20min）。",
            },
            "connection_id": {
                "type": "string",
                "description": "可选。指定使用哪个 connection；不填走当前会话默认。",
            },
        },
        "required": ["mode", "host_query", "metric"],
    },
}


def _extract_signals(result: dict) -> list[dict]:
    """peak 模式下峰值过高时挂 signal，让 agent 跨域 pivot。

    （函数名 + 签名跟旧 metric_query_peak 一致，test_skill_signals 复用。）

    设计要点：
    - host_name 为空时**不挂 next_skill**——免得给模型 ``{"host_query": ""}`` 的无效
      next_args；只发证据让模型自己消化。
    - warning 阈值（80~90%）也挂 next_skill，否则该区间模型拿到 signal 没 pivot 提示。
    """
    peak = result.get("peak") or {}
    metric = result.get("metric")
    value = peak.get("value")
    host_name = (result.get("host") or {}).get("host_name") or ""
    sig_type = _METRIC_TO_SIGNAL.get(metric)

    if not (sig_type and isinstance(value, (int, float))):
        return []

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
                **pivot,
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
    mode: str,
    host_query: str,
    metric: str,
    # peak
    lookback_hours: float = 24,
    direction: str = "max",
    context_window_seconds: int = DEFAULT_PEAK_CONTEXT_SECONDS,
    # window
    center_time: str | int | float | None = None,
    half_width_minutes: float = 10,
    connection_id: str | None = None,
) -> dict:
    if metric not in KNOWN_METRICS:
        raise ValueError(
            f"未知 metric: {metric}（支持的：{', '.join(KNOWN_METRICS)}）"
        )
    if mode not in ("peak", "window"):
        raise ValueError(f"未知 mode: {mode}（支持 peak / window）")

    provider = ctx.connection_for("zabbix", connection_id)

    if mode == "peak":
        lookback_seconds = max(1, int(float(lookback_hours) * 3600))
        if lookback_seconds > 30 * 24 * 3600:
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

    # mode == "window"
    half_width_seconds = max(60, int(float(half_width_minutes) * 60))
    if half_width_seconds > 6 * 3600:
        half_width_seconds = 6 * 3600
    return fetch_window(
        provider,
        host_query,
        metric,
        center_time if center_time not in (None, "") else None,
        half_width_seconds=half_width_seconds,
    )
