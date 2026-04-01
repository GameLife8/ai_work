from __future__ import annotations

from services.alert_parser import AlertParser


def test_parses_disk_notification_from_mobile_style_payload():
    payload = {
        "alert_level": "/mnt/data01: 磁盘空间严重不足 (used > 90%)",
        "alert_detail": "WYY-DB09 (169.24.7.117) Resolved in 2026.04.01 11:25:15",
        "event_time": "2026-04-01 11:25:25",
    }

    alert = AlertParser.parse(payload)

    assert alert["host_name"] == "WYY-DB09"
    assert alert["host_ip"] == "169.24.7.117"
    assert alert["alert_type"] == "disk"
    assert alert["resource_scope"]["mount_point"] == "/mnt/data01"
    assert alert["signal"]["threshold_percent"] == 90
    assert alert["status"] == "resolved"


def test_parses_cpu_notification_threshold_and_duration():
    payload = {
        "alert_level": "高 CPU 利用率 (over 90% for 5m)",
        "alert_detail": "Problem: P-L-TIDB09 (169.24.7.26) in 2026.03.31 10:10:48",
    }

    alert = AlertParser.parse(payload)

    assert alert["alert_type"] == "cpu"
    assert alert["host_name"] == "P-L-TIDB09"
    assert alert["host_ip"] == "169.24.7.26"
    assert alert["signal"]["threshold_percent"] == 90
    assert alert["signal"]["duration_minutes"] == 5
    assert alert["status"] == "problem"
