from __future__ import annotations


def test_healthcheck_returns_storage_status(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["storage"]["backend"] == "memory"
    assert body["storage"]["healthy"] is True


def test_storage_status_endpoint_returns_backend_details(client):
    response = client.get("/api/v1/system/storage")

    assert response.status_code == 200
    body = response.get_json()
    assert body["backend"] == "memory"
    assert body["healthy"] is True
