from __future__ import annotations

from services.zabbix_client import SAMPLE_INTERVAL_SECONDS
from services.zabbix_client import ZabbixClient


def test_zabbix_client_stub_healthcheck_is_healthy():
    client = ZabbixClient(base_url="http://example.com", use_stub=True)

    result = client.healthcheck()

    assert result["healthy"] is True
    assert result["backend"] == "stub"


def test_zabbix_client_requires_credentials_when_stub_disabled():
    client = ZabbixClient(base_url="http://example.com", use_stub=False)

    try:
        client.login()
    except ValueError as exc:
        assert "credentials" in str(exc).lower()
    else:
        raise AssertionError("Expected ValueError when credentials are missing")


def test_build_sample_times_uses_one_hour_window_with_twelve_points():
    client = ZabbixClient(base_url="http://example.com", use_stub=False)

    sample_times = client._build_sample_times({"event_time": "2026-04-02 12:00:00"})

    assert len(sample_times) == 12
    assert sample_times[-1] - sample_times[0] == SAMPLE_INTERVAL_SECONDS * 11
    assert sample_times[1] - sample_times[0] == SAMPLE_INTERVAL_SECONDS
