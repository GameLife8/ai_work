"""Tests for ZabbixClient's MetricProvider implementation.

These cover the adapter seam — host resolution, logical metric -> Zabbix
item mapping, and history.get -> MetricPoint conversion. RPC is mocked
out by overriding ``_rpc`` / ``_get_host_items`` / ``find_host`` on a
subclass, mirroring the pattern in test_zabbix_client_memory.py.
"""

from __future__ import annotations

from services.metric_provider import (
    HostRef,
    MetricDescriptor,
    MetricPoint,
    MetricProvider,
)
from services.zabbix_client import ZabbixClient


def test_zabbix_client_satisfies_metric_provider_protocol():
    """非 stub 也要满足 protocol（runtime_checkable Protocol）。"""
    client = ZabbixClient(base_url="http://x", use_stub=False)
    assert isinstance(client, MetricProvider)
    assert client.name == "zabbix"


def test_resolve_host_in_stub_mode_returns_hostref():
    client = ZabbixClient(base_url="http://x", use_stub=True)
    host = client.resolve_host("web-01")
    assert isinstance(host, HostRef)
    assert host.name == "web-01"
    assert host.extra.get("_stub") is True


def test_find_metric_returns_none_for_unknown_logical_name():
    client = ZabbixClient(base_url="http://x", use_stub=True)
    host = HostRef(id="h1", name="web-01")
    assert client.find_metric(host, "made.up.metric") is None


def test_find_metric_in_stub_mode_returns_descriptor():
    client = ZabbixClient(base_url="http://x", use_stub=True)
    host = HostRef(id="h1", name="web-01")
    metric = client.find_metric(host, "cpu.utilization")
    assert isinstance(metric, MetricDescriptor)
    assert metric.name == "cpu.utilization"


def test_query_series_in_stub_mode_returns_increasing_timestamps():
    client = ZabbixClient(base_url="http://x", use_stub=True)
    host = HostRef(id="h1", name="web-01")
    metric = client.find_metric(host, "cpu.utilization")

    series = client.query_series(host, metric, 1_700_000_000, 1_700_003_600)
    assert len(series) > 0
    timestamps = [p.timestamp for p in series]
    assert timestamps == sorted(timestamps)
    assert all(isinstance(p, MetricPoint) for p in series)
    assert all(1_700_000_000 <= p.timestamp <= 1_700_003_600 for p in series)


def test_query_series_returns_empty_for_inverted_window():
    client = ZabbixClient(base_url="http://x", use_stub=True)
    host = HostRef(id="h1", name="web-01")
    metric = client.find_metric(host, "cpu.utilization")
    assert client.query_series(host, metric, 100, 50) == []


class _ApiMockClient(ZabbixClient):
    """非 stub 模式下，把 _rpc / _get_host_items / find_host mock 掉，
    验证 history.get 的参数 + 解析逻辑。"""

    def __init__(self):
        super().__init__(base_url="http://x", use_stub=False)
        self._auth_token = "fake-token"
        self.last_rpc_call: dict | None = None

    def find_host(self, query):
        return {
            "hostid": "12345", "host": "web-01.lan", "name": query,
            "interfaces": [{"ip": "10.0.0.1"}],
        }

    def _get_host_items(self, host_id):
        return [
            {"itemid": "777", "key_": "system.cpu.util[,system,avg1]",
             "name": "CPU util", "value_type": "0", "units": "%"},
            {"itemid": "888", "key_": "vm.memory.utilization",
             "name": "Memory", "value_type": "0", "units": "%"},
        ]

    def _rpc(self, method, params, auth=None):
        self.last_rpc_call = {"method": method, "params": params, "auth": auth}
        if method == "history.get":
            return [
                {"clock": "1700000060", "value": "73.5"},
                {"clock": "1700000120", "value": "81.2"},
                {"clock": "bad",        "value": "ignored"},   # malformed row
                {"clock": "1700000180", "value": "70.1"},
            ]
        return None


def test_resolve_host_returns_canonical_hostref_in_api_mode():
    client = _ApiMockClient()
    host = client.resolve_host("web-01")
    assert host.id == "12345"
    assert host.name == "web-01"
    assert host.ip == "10.0.0.1"


def test_find_metric_maps_cpu_utilization_to_zabbix_item():
    client = _ApiMockClient()
    host = HostRef(id="12345", name="web-01")
    metric = client.find_metric(host, "cpu.utilization")
    assert metric is not None
    assert metric.unit == "%"
    assert metric.provider_handle["itemid"] == "777"
    assert metric.provider_handle["value_type"] == 0


def test_query_series_calls_history_get_with_correct_window():
    client = _ApiMockClient()
    host = HostRef(id="12345", name="web-01")
    metric = client.find_metric(host, "cpu.utilization")

    series = client.query_series(host, metric, 1_700_000_000, 1_700_000_300)

    assert client.last_rpc_call["method"] == "history.get"
    params = client.last_rpc_call["params"]
    assert params["itemids"] == ["777"]
    assert params["history"] == 0
    assert params["time_from"] == 1_700_000_000
    assert params["time_till"] == 1_700_000_300
    assert params["sortorder"] == "ASC"

    # malformed row dropped silently, three good points retained
    assert len(series) == 3
    assert [p.value for p in series] == [73.5, 81.2, 70.1]
    assert [p.timestamp for p in series] == [1_700_000_060, 1_700_000_120, 1_700_000_180]


def test_analytics_find_peak_works_through_zabbix_adapter():
    """Smoke test: full pipe analytics(find_peak) -> ZabbixClient -> mocked RPC."""
    from services.metric_analytics import find_peak

    client = _ApiMockClient()
    result = find_peak(
        client, "web-01", "cpu.utilization",
        lookback_seconds=3600, end_ts=1_700_003_600,
    )
    assert result["peak"]["value"] == 81.2
    assert result["peak"]["timestamp"] == 1_700_000_120
    assert result["host"]["host_id"] == "12345"
