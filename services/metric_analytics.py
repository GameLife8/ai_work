"""Provider-agnostic 指标分析规则。

定位
----
"指标查询规则" 和 "怎么调监控产品 API" 是两件事——本模块只做前者：

    find_peak(provider, host, metric, lookback)
        最近 N 小时内出现的最大/最小值，附峰值时刻 ±N 秒上下文 + 全局统计

    fetch_window(provider, host, metric, center, half_width)
        指定时间点 ±N 秒内的全部原始采样（精确时间排查用）

    summarize(provider, host, metric, lookback)
        窗口内的 avg / max / min / p95 / last 聚合 + 原始点 count

所有函数只通过 ``MetricProvider`` 协议跟具体监控产品打交道——换 Prometheus、
Datadog、自研 Agent 都不用改这里一行代码。

时间约定
--------
- 内部全部用 epoch seconds (UTC)。
- ``center`` 参数支持 epoch int/float、ISO 字符串、``%Y-%m-%d %H:%M:%S``、
  Python ``datetime``；本地时区按 ``DEFAULT_TIMEZONE``（默认 Asia/Shanghai）解析。
- 返回的 dict 同时给 ``timestamp`` (epoch) 和 ``time`` (UTC ISO)；前端渲染再转本地。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from services.metric_provider import (
    HostRef,
    MetricDescriptor,
    MetricPoint,
    MetricProvider,
)


logger = logging.getLogger(__name__)


# 解析无时区字符串时假设的本地时区（跟 zabbix_client._parse_event_time 保持一致）
DEFAULT_TIMEZONE = ZoneInfo("Asia/Shanghai")

# 默认窗口
DEFAULT_PEAK_LOOKBACK_SECONDS = 24 * 3600
DEFAULT_PEAK_CONTEXT_SECONDS = 300            # 峰值前后各 ±150s 的原始点
DEFAULT_WINDOW_HALF_WIDTH_SECONDS = 600       # ±10min（用户给精确时间时默认值）


# ----------------------------- 时间解析 ---------------------------------- #


def parse_timestamp(value: Any) -> int:
    """把多种时间表示统一成 UTC epoch seconds。

    支持：
        None / 空字符串 → now
        int / float    → 直接当 epoch（>=1e12 视为毫秒，自动 /1000）
        datetime       → 没 tzinfo 当作 DEFAULT_TIMEZONE
        ISO 字符串      → datetime.fromisoformat
        %Y-%m-%d %H:%M:%S / %Y-%m-%d %H:%M / %Y/%m/%d %H:%M:%S → strptime

    解析失败抛 ``ValueError``——调用方可以回退到 now 或上抛给用户。
    """
    if value is None:
        return _now_ts()
    if isinstance(value, bool):
        # bool 是 int 的子类；显式拒绝避免 True 被当作 epoch=1
        raise ValueError(f"Unrecognized timestamp: {value!r}")
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts >= 1e12:        # 毫秒级
            ts /= 1000.0
        return int(ts)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=DEFAULT_TIMEZONE)
        return int(value.astimezone(UTC).timestamp())

    text = str(value).strip()
    if not text:
        return _now_ts()

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None

    if parsed is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y.%m.%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(f"Unrecognized timestamp: {value!r}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DEFAULT_TIMEZONE)
    return int(parsed.astimezone(UTC).timestamp())


def _now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


# --------------------------- 内部小工具 ---------------------------------- #


def _format_point(point: MetricPoint, *, precision: int = 6) -> dict[str, Any]:
    return {
        "timestamp": point.timestamp,
        "time": datetime.fromtimestamp(point.timestamp, UTC).isoformat(),
        "value": round(point.value, precision),
    }


def _host_dict(host: HostRef) -> dict[str, Any]:
    return {
        "host_id": host.id,
        "host_name": host.name,
        "host_ip": host.ip,
    }


def _resolve_host_strict(provider: MetricProvider, host_query: str) -> HostRef:
    host = provider.resolve_host(host_query)
    if not host:
        raise ValueError(f"未找到主机：{host_query}（provider={getattr(provider, 'name', '?')}）")
    return host


def _find_metric_strict(
    provider: MetricProvider, host: HostRef, metric_name: str,
) -> MetricDescriptor:
    metric = provider.find_metric(host, metric_name)
    if not metric:
        raise ValueError(
            f"主机 {host.name} 上未发现指标 {metric_name}"
            f"（provider={getattr(provider, 'name', '?')}）"
        )
    return metric


def _aggregate(values: list[float], *, precision: int = 4) -> dict[str, Any]:
    """avg / max / min / p95 / last。

    p95 用 nearest-rank：``sorted[ceil(0.95 * n) - 1]``，跟
    ``ZabbixClient._aggregate`` 算法一致——保证两条路径数字对得上。
    """
    if not values:
        return {}
    sorted_values = sorted(values)
    p95_idx = min(len(sorted_values) - 1, max(0, int(round(0.95 * len(sorted_values))) - 1))
    return {
        "avg": round(mean(values), precision),
        "max": round(max(values), precision),
        "min": round(min(values), precision),
        "p95": round(sorted_values[p95_idx], precision),
        "last": round(values[-1], precision),
        "raw_count": len(values),
    }


def _slice_around(
    series: list[MetricPoint], center_ts: int, half_width_seconds: int,
) -> list[MetricPoint]:
    """series 已按时间升序，取 [center - half, center + half] 范围。"""
    half = max(0, int(half_width_seconds))
    lo, hi = center_ts - half, center_ts + half
    return [p for p in series if lo <= p.timestamp <= hi]


# ---------------------------- 公共规则 ----------------------------------- #


def find_peak(
    provider: MetricProvider,
    host_query: str,
    metric_name: str,
    *,
    lookback_seconds: int = DEFAULT_PEAK_LOOKBACK_SECONDS,
    direction: str = "max",
    context_window_seconds: int = DEFAULT_PEAK_CONTEXT_SECONDS,
    end_ts: int | None = None,
) -> dict[str, Any]:
    """在 [end - lookback, end] 区间内找最大/最小值。

    Returns:
        {
          "host":            {host_id, host_name, host_ip},
          "metric":          逻辑名,
          "unit":            provider 给的单位,
          "direction":       "max" | "min",
          "lookback_seconds": int,
          "window":          {start_timestamp, start_time, end_timestamp, end_time},
          "peak":            {timestamp, time, value} | None,
          "context_samples": [{timestamp, time, value}, ...]   # 峰值附近原始点
          "stats":           {avg, max, min, p95, last, raw_count},
          "warning":         (可选) "窗口内无数据" 等提示
        }
    """
    if direction not in ("max", "min"):
        raise ValueError("direction must be 'max' or 'min'")
    if lookback_seconds <= 0:
        raise ValueError("lookback_seconds must be > 0")

    host = _resolve_host_strict(provider, host_query)
    metric = _find_metric_strict(provider, host, metric_name)

    end = int(end_ts) if end_ts is not None else _now_ts()
    start = end - int(lookback_seconds)
    series = provider.query_series(host, metric, start, end)

    base = {
        "host": _host_dict(host),
        "metric": metric_name,
        "unit": metric.unit,
        "direction": direction,
        "lookback_seconds": int(lookback_seconds),
        "window": {
            "start_timestamp": start,
            "start_time": datetime.fromtimestamp(start, UTC).isoformat(),
            "end_timestamp": end,
            "end_time": datetime.fromtimestamp(end, UTC).isoformat(),
        },
    }

    if not series:
        return {
            **base,
            "peak": None,
            "context_samples": [],
            "stats": {},
            "warning": f"窗口内（{lookback_seconds}s）无 {metric_name} 监控数据",
        }

    pick = max if direction == "max" else min
    peak_point = pick(series, key=lambda p: p.value)
    context = _slice_around(series, peak_point.timestamp, context_window_seconds // 2)
    return {
        **base,
        "peak": _format_point(peak_point),
        "context_samples": [_format_point(p) for p in context],
        "stats": _aggregate([p.value for p in series]),
    }


def fetch_window(
    provider: MetricProvider,
    host_query: str,
    metric_name: str,
    center: Any,
    *,
    half_width_seconds: int = DEFAULT_WINDOW_HALF_WIDTH_SECONDS,
) -> dict[str, Any]:
    """取 ``[center - half, center + half]`` 窗口内的全部原始采样。

    用户给精确时间排查时用——例：发现 12:34:00 有 spike，调
    ``fetch_window(..., center='2026-04-02 12:34:00', half_width_seconds=600)``
    返回 12:24:00 ~ 12:44:00 的全部原始点。

    "每秒一个点" 是上限，实际粒度看 provider agent 采集频率（Zabbix 通常 1 分钟，
    Prometheus 通常 15 秒）；本函数不主动重采样——返回 provider 原汁原味的
    时间序列，避免插值误导。
    """
    if half_width_seconds <= 0:
        raise ValueError("half_width_seconds must be > 0")

    host = _resolve_host_strict(provider, host_query)
    metric = _find_metric_strict(provider, host, metric_name)

    center_ts = parse_timestamp(center)
    half = int(half_width_seconds)
    start = center_ts - half
    end = center_ts + half

    series = provider.query_series(host, metric, start, end)
    samples = [_format_point(p) for p in series]

    return {
        "host": _host_dict(host),
        "metric": metric_name,
        "unit": metric.unit,
        "center_timestamp": center_ts,
        "center_time": datetime.fromtimestamp(center_ts, UTC).isoformat(),
        "half_width_seconds": half,
        "window": {
            "start_timestamp": start,
            "start_time": datetime.fromtimestamp(start, UTC).isoformat(),
            "end_timestamp": end,
            "end_time": datetime.fromtimestamp(end, UTC).isoformat(),
        },
        "samples": samples,
        "sample_count": len(samples),
        "stats": _aggregate([p.value for p in series]) if series else {},
    }


def summarize(
    provider: MetricProvider,
    host_query: str,
    metric_name: str,
    *,
    lookback_seconds: int = DEFAULT_PEAK_LOOKBACK_SECONDS,
    end_ts: int | None = None,
) -> dict[str, Any]:
    """窗口内的 avg / max / min / p95 / last + raw_count。

    跟 ``find_peak`` 区别：不挑出单个峰值时刻，只回统计量；适合
    "过去一天 CPU 平均怎么样" 这种问题。
    """
    if lookback_seconds <= 0:
        raise ValueError("lookback_seconds must be > 0")

    host = _resolve_host_strict(provider, host_query)
    metric = _find_metric_strict(provider, host, metric_name)

    end = int(end_ts) if end_ts is not None else _now_ts()
    start = end - int(lookback_seconds)
    series = provider.query_series(host, metric, start, end)
    return {
        "host": _host_dict(host),
        "metric": metric_name,
        "unit": metric.unit,
        "lookback_seconds": int(lookback_seconds),
        "window": {
            "start_timestamp": start,
            "start_time": datetime.fromtimestamp(start, UTC).isoformat(),
            "end_timestamp": end,
            "end_time": datetime.fromtimestamp(end, UTC).isoformat(),
        },
        "stats": _aggregate([p.value for p in series]) if series else {},
        "sample_count": len(series),
    }
