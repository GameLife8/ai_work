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


def test_cleanup_endpoint_clears_runtime_data(client):
    client.post(
        "/api/v1/alerts/zabbix",
        json={
            "event_id": "cleanup-1",
            "problem_id": "cleanup-p1",
            "host_name": "cleanup-host",
            "host_ip": "10.0.0.99",
            "severity": "high",
            "status": "problem",
            "alert_name": "CPU usage > 90%",
            "message": "CPU usage high",
            "event_time": "2026-04-01T10:02:00",
        },
    )

    response = client.post("/api/v1/system/storage/cleanup", json={"confirm": "yes"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["code"] == 0
    assert body["cleared"]["alert_events"] >= 1

    storage = client.get("/api/v1/system/storage").get_json()
    assert storage["details"]["alert_events"] == 0
