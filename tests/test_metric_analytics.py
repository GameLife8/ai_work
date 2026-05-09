"""Pure-rules tests for services.metric_analytics — no Zabbix involved.

A fake provider is fed deterministic data; we assert peak detection,
window slicing, time parsing, and stat aggregation behave as expected.
The same tests would catch regressions if we later port the rules to
Prometheus / Datadog providers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone, timedelta

import pytest

from services.metric_analytics import (
    DEFAULT_PEAK_LOOKBACK_SECONDS,
    fetch_window,
    find_peak,
    parse_timestamp,
    summarize,
)
from services.metric_provider import (
    HostRef,
    MetricDescriptor,
    MetricPoint,
    MetricProvider,
)


class FakeProvider:
    """In-memory MetricProvider for unit tests.

    Stored series is keyed by (host_id, metric_name) and must be
    sorted by timestamp ASC — that's what real providers promise.
    """

    name = "fake"

    def __init__(self) -> None:
        self.hosts: dict[str, HostRef] = {}
        self.metrics: dict[tuple[str, str], MetricDescriptor] = {}
        self.series: dict[tuple[str, str], list[MetricPoint]] = {}

    def add_host(self, host: HostRef) -> None:
        self.hosts[host.name] = host

    def add_metric(
        self, host_name: str, metric_name: str, points: list[tuple[int, float]],
        unit: str = "%",
    ) -> None:
        host = self.hosts[host_name]
        self.metrics[(host.id, metric_name)] = MetricDescriptor(
            name=metric_name, unit=unit, provider_handle={"key": metric_name},
        )
        self.series[(host.id, metric_name)] = [
            MetricPoint(timestamp=t, value=v) for t, v in sorted(points)
        ]

    def resolve_host(self, query):
        return self.hosts.get(query)

    def find_metric(self, host, metric_name):
        return self.metrics.get((host.id, metric_name))

    def query_series(self, host, metric, start_ts, end_ts):
        rows = self.series.get((host.id, metric.name), [])
        return [p for p in rows if start_ts <= p.timestamp <= end_ts]


@pytest.fixture()
def provider() -> FakeProvider:
    p = FakeProvider()
    p.add_host(HostRef(id="h1", name="web-01", ip="10.0.0.1"))
    return p


def test_provider_implements_protocol(provider):
    """FakeProvider 实例必须能被 isinstance 识别为 MetricProvider"""
    assert isinstance(provider, MetricProvider)


def test_find_peak_returns_global_max_with_context(provider):
    base = 1_700_000_000
    points = [(base + i * 60, 50.0 + i) for i in range(10)]   # 50, 51, ..., 59
    points.append((base + 5 * 60, 95.0))                       # spike
    provider.add_metric("web-01", "cpu.utilization", points)

    result = find_peak(
        provider, "web-01", "cpu.utilization",
        lookback_seconds=3600, end_ts=base + 3600,
        context_window_seconds=300,
    )

    assert result["peak"]["value"] == 95.0
    assert result["peak"]["timestamp"] == base + 5 * 60
    assert result["stats"]["max"] == 95.0
    assert result["stats"]["raw_count"] == 11
    # context window is ±150s around peak, so should pick a few neighbors
    assert len(result["context_samples"]) >= 1
    assert result["host"]["host_id"] == "h1"
    assert result["unit"] == "%"


def test_find_peak_min_direction(provider):
    base = 1_700_000_000
    points = [(base + i * 60, 70.0 - i) for i in range(5)]    # 70..66
    provider.add_metric("web-01", "memory.utilization", points)

    result = find_peak(
        provider, "web-01", "memory.utilization",
        lookback_seconds=3600, end_ts=base + 3600,
        direction="min",
    )

    assert result["direction"] == "min"
    assert result["peak"]["value"] == 66.0


def test_find_peak_warns_on_empty_window(provider):
    provider.add_metric("web-01", "cpu.utilization", [])
    result = find_peak(
        provider, "web-01", "cpu.utilization",
        lookback_seconds=3600,
    )
    assert result["peak"] is None
    assert "无" in result["warning"]
    assert result["stats"] == {}


def test_find_peak_unknown_host_raises(provider):
    with pytest.raises(ValueError, match="未找到主机"):
        find_peak(provider, "nope", "cpu.utilization", lookback_seconds=60)


def test_find_peak_unknown_metric_raises(provider):
    with pytest.raises(ValueError, match="未发现指标"):
        find_peak(provider, "web-01", "made.up.metric", lookback_seconds=60)


def test_find_peak_validates_direction(provider):
    provider.add_metric("web-01", "cpu.utilization", [(1, 1.0)])
    with pytest.raises(ValueError, match="direction"):
        find_peak(provider, "web-01", "cpu.utilization",
                  lookback_seconds=60, direction="weird")


def test_fetch_window_slices_around_center(provider):
    base = 1_700_000_000
    # 30 个点，每分钟一个，总共 30min
    points = [(base + i * 60, 50.0 + i) for i in range(30)]
    provider.add_metric("web-01", "cpu.utilization", points)

    # center 在第 15 个点（base + 900s），±10min 应抓到 [5..25]
    result = fetch_window(
        provider, "web-01", "cpu.utilization",
        base + 15 * 60, half_width_seconds=600,
    )

    timestamps = [s["timestamp"] for s in result["samples"]]
    assert timestamps[0] >= base + 5 * 60
    assert timestamps[-1] <= base + 25 * 60
    assert result["center_timestamp"] == base + 15 * 60
    assert result["sample_count"] == len(result["samples"])
    assert result["window"]["start_timestamp"] == base + 15 * 60 - 600
    assert result["window"]["end_timestamp"] == base + 15 * 60 + 600


def test_fetch_window_accepts_iso_with_tz(provider):
    """ISO with explicit +08:00 tz must convert to UTC consistently."""
    # ISO 2026-04-02T12:00:00+08:00 → UTC 2026-04-02T04:00:00
    expected_utc_ts = int(datetime(2026, 4, 2, 4, 0, 0, tzinfo=UTC).timestamp())
    provider.add_metric("web-01", "cpu.utilization",
                        [(expected_utc_ts, 73.0)])

    result = fetch_window(
        provider, "web-01", "cpu.utilization",
        "2026-04-02T12:00:00+08:00", half_width_seconds=60,
    )
    assert result["center_timestamp"] == expected_utc_ts
    assert len(result["samples"]) == 1


def test_fetch_window_naive_string_treated_as_shanghai(provider):
    """不带时区的字符串按 Asia/Shanghai 解析（与 zabbix_client 行为一致）。"""
    expected_utc_ts = int(datetime(2026, 4, 2, 4, 0, 0, tzinfo=UTC).timestamp())
    provider.add_metric("web-01", "cpu.utilization", [(expected_utc_ts, 1.0)])

    result = fetch_window(
        provider, "web-01", "cpu.utilization",
        "2026-04-02 12:00:00", half_width_seconds=60,
    )
    assert result["center_timestamp"] == expected_utc_ts


def test_fetch_window_validates_half_width(provider):
    provider.add_metric("web-01", "cpu.utilization", [(1, 1.0)])
    with pytest.raises(ValueError, match="half_width_seconds"):
        fetch_window(provider, "web-01", "cpu.utilization", 1, half_width_seconds=0)


def test_summarize_returns_full_stats(provider):
    base = 1_700_000_000
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    points = [(base + i * 60, v) for i, v in enumerate(values)]
    provider.add_metric("web-01", "cpu.utilization", points)

    result = summarize(
        provider, "web-01", "cpu.utilization",
        lookback_seconds=3600, end_ts=base + 3600,
    )

    assert result["stats"]["min"] == 10.0
    assert result["stats"]["max"] == 100.0
    assert result["stats"]["avg"] == 55.0
    assert result["stats"]["last"] == 100.0
    assert result["stats"]["raw_count"] == 10
    assert result["sample_count"] == 10


def test_parse_timestamp_handles_epoch_seconds_and_ms():
    assert parse_timestamp(1_700_000_000) == 1_700_000_000
    assert parse_timestamp(1_700_000_000_000) == 1_700_000_000


def test_parse_timestamp_handles_datetime_with_and_without_tz():
    # naive — assume Asia/Shanghai
    naive = datetime(2026, 4, 2, 12, 0, 0)
    assert parse_timestamp(naive) == int(datetime(2026, 4, 2, 4, 0, 0, tzinfo=UTC).timestamp())
    # aware — respect provided offset
    aware = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    assert parse_timestamp(aware) == int(datetime(2026, 4, 2, 3, 0, 0, tzinfo=UTC).timestamp())


def test_parse_timestamp_rejects_bool_and_nonsense():
    with pytest.raises(ValueError):
        parse_timestamp(True)
    with pytest.raises(ValueError):
        parse_timestamp("not a date")


def test_parse_timestamp_handles_empty_string_as_now():
    # empty string → now (just sanity-check the type, value drifts)
    assert isinstance(parse_timestamp(""), int)
    assert isinstance(parse_timestamp(None), int)
