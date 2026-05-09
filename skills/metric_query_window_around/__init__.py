"""Skill: 取指定时刻 ±N 分钟内的全部原始采样。

回答 "12:34 那会儿 CPU 是多少"、"故障时刻前后 10 分钟内存波动" 之类问题。

设计要点
--------
- 规则在 ``services.metric_analytics.fetch_window``——provider-agnostic。
- 返回的是 provider 原汁原味的采样（不重采样、不插值），粒度由后端决定：
  Zabbix 通常 60s 一个点，Prometheus 通常 15s。skill 文档明确不承诺"每秒"。
- 用户给的时间字符串支持 ISO（2026-04-02T12:34:00）+ 常见格式
  （%Y-%m-%d %H:%M:%S 等），无时区时按 Asia/Shanghai 解析。
"""

from __future__ import annotations

import logging

from services.metric_analytics import (
    DEFAULT_WINDOW_HALF_WIDTH_SECONDS,
    fetch_window,
)
from services.metric_provider import KNOWN_METRICS


logger = logging.getLogger(__name__)


MANIFEST = {
    "code": "metric_query_window_around",
    "name": "查询指定时刻附近的原始采样",
    "description": (
        "取指定主机某项指标在用户给定时刻 **±N 分钟内的全部原始采样**（默认 ±10 分钟），"
        "用来精确还原故障/告警时刻附近的曲线。"
        "**典型场景**："
        "(1) 用户问「12 点半左右 CPU 是多少」「告警时刻前后内存波动如何」——直接调本 skill；"
        "(2) 配合 metric_query_peak 找到峰值时刻，再用本 skill 拉峰值前后细粒度数据；"
        "(3) 跟应用日志时间对齐——确认监控指标和应用日志事件的时间相关性。"
        "返回的 samples 是 provider 原始粒度（Zabbix 通常 60s 一个点，**不是真正每秒一个点**），"
        "如果需要更细粒度，要在 agent 端调高采集频率，平台无法在事后插值补点。"
        "支持的 metric 跟 metric_query_peak 一致：cpu.utilization / memory.utilization / system.load.avg1。"
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
                "description": "主机名 / IP / hostid。",
            },
            "metric": {
                "type": "string",
                "enum": list(KNOWN_METRICS),
                "description": "逻辑指标名。CPU 用率传 cpu.utilization，内存用率传 memory.utilization。",
            },
            "center_time": {
                "type": "string",
                "description": (
                    "中心时间。支持 ISO 8601（2026-04-02T12:34:00+08:00）、"
                    "「%Y-%m-%d %H:%M:%S」、epoch 秒/毫秒。"
                    "无时区时按 Asia/Shanghai 解析。"
                    "不传或空字符串则使用当前时间（等价于「最近 ±N 分钟」）。"
                ),
            },
            "half_width_minutes": {
                "type": "number",
                "default": 10,
                "minimum": 1,
                "maximum": 360,
                "description": "中心时间前后各取多少分钟。默认 10（即 ±10min，整窗口 20min）。",
            },
            "connection_id": {
                "type": "string",
                "description": "可选。指定使用哪个 connection。",
            },
        },
        "required": ["host_query", "metric"],
    },
}


def run(
    ctx,
    *,
    host_query: str,
    metric: str,
    center_time: str | int | float | None = None,
    half_width_minutes: float = 10,
    connection_id: str | None = None,
) -> dict:
    if metric not in KNOWN_METRICS:
        raise ValueError(
            f"未知 metric: {metric}（支持的：{', '.join(KNOWN_METRICS)}）"
        )

    provider = ctx.connection_for("zabbix", connection_id)
    half_width_seconds = max(60, int(float(half_width_minutes) * 60))
    if half_width_seconds > 6 * 3600:
        half_width_seconds = 6 * 3600       # cap 到 ±6h，跟 manifest schema 一致

    return fetch_window(
        provider,
        host_query,
        metric,
        center_time if center_time not in (None, "") else None,
        half_width_seconds=half_width_seconds,
    )
