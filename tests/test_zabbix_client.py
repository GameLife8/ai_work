from __future__ import annotations

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
