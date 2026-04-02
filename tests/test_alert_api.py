from __future__ import annotations


def _sample_payload(**overrides):
    payload = {
        "event_id": "123456",
        "problem_id": "654321",
        "trigger_id": "98765",
        "host_id": "10001",
        "host_name": "app-prod-01",
        "host_ip": "10.0.0.12",
        "severity": "high",
        "status": "problem",
        "alert_name": "CPU usage > 90%",
        "message": "CPU usage high",
        "tags": {"env": "prod", "service": "order-api"},
        "event_time": "2026-04-01T10:02:00",
    }
    payload.update(overrides)
    return payload


def test_accepts_zabbix_alert_and_creates_incident(client):
    response = client.post("/api/v1/alerts/zabbix", json=_sample_payload())

    assert response.status_code == 200
    body = response.get_json()
    assert body["code"] == 0
    assert body["alert_id"] == 1
    assert body["decision"]["decision"] == "notify"
    assert body["decision"]["need_push"] is True
    assert body["decision"]["transfer_to_ticket"] is True
    assert body["decision"]["ticket_status"] == "pending"
    assert body["decision"]["incident_no"].startswith("INC")


def test_merges_when_related_incident_exists(client):
    first = client.post("/api/v1/alerts/zabbix", json=_sample_payload())
    assert first.status_code == 200

    second = client.post(
        "/api/v1/alerts/zabbix",
        json=_sample_payload(event_id="123457", problem_id="654322"),
    )

    assert second.status_code == 200
    body = second.get_json()
    assert body["decision"]["decision"] == "merge"
    assert "merge_target_incident_no" in body["decision"]
    assert body["decision"]["need_push"] is False
    assert body["decision"]["push_suppressed"] is True


def test_repeats_push_after_24_hours_if_incident_still_open(client):
    first = client.post("/api/v1/alerts/zabbix", json=_sample_payload(event_time="2026-04-01T10:02:00"))
    assert first.status_code == 200

    second = client.post(
        "/api/v1/alerts/zabbix",
        json=_sample_payload(event_id="123458", problem_id="654323", event_time="2026-04-02T10:03:00"),
    )

    assert second.status_code == 200
    body = second.get_json()
    assert body["decision"]["decision"] == "merge"
    assert body["decision"]["need_push"] is True
    assert body["decision"]["push_count"] == 2


def test_resolved_alert_closes_incident_and_resets_future_push(client):
    first = client.post("/api/v1/alerts/zabbix", json=_sample_payload(event_time="2026-04-01T10:02:00"))
    assert first.status_code == 200

    resolved = client.post(
        "/api/v1/alerts/zabbix",
        json=_sample_payload(
            event_id="123459",
            problem_id="654324",
            status="resolved",
            event_time="2026-04-01T11:02:00",
        ),
    )
    assert resolved.status_code == 200
    assert resolved.get_json()["decision"]["ticket_status"] == "resolved"

    third = client.post(
        "/api/v1/alerts/zabbix",
        json=_sample_payload(
            event_id="123460",
            problem_id="654325",
            status="problem",
            event_time="2026-04-01T12:02:00",
        ),
    )

    assert third.status_code == 200
    body = third.get_json()
    assert body["decision"]["decision"] == "notify"
    assert body["decision"]["need_push"] is True
    assert body["decision"]["push_count"] == 1


def test_rejects_invalid_payload(client):
    response = client.post("/api/v1/alerts/zabbix", data="not-json", content_type="text/plain")

    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == 400


def test_accepts_notification_style_payload(client):
    response = client.post(
        "/api/v1/alerts/notification",
        json={
            "to_user": "77301",
            "subject": "Zabbix告警通知",
            "alert_message": "/mnt/data01: 磁盘空间严重不足 (used > 90%)",
            "alert_detail": "WYY-DB09 (169.24.7.117) Problem in 2026.04.01 11:25:15",
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["code"] == 0
    assert body["decision"]["decision"] in {"notify", "observe"}
    assert "plan" in body
    assert "context" in body
    assert "report" in body["decision"]
