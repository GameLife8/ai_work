from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime


class AlertParser:
    @staticmethod
    def parse(raw_payload: dict) -> dict:
        tags = raw_payload.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}

        event_time = raw_payload.get("event_time") or datetime.now(UTC).isoformat()

        return {
            "source": "zabbix",
            "source_event_id": str(raw_payload.get("event_id", "")),
            "source_problem_id": str(raw_payload.get("problem_id", "")),
            "trigger_id": str(raw_payload.get("trigger_id", "")),
            "host_id": str(raw_payload.get("host_id", "")),
            "host_name": raw_payload.get("host_name", ""),
            "host_ip": raw_payload.get("host_ip", ""),
            "severity": raw_payload.get("severity", "unknown"),
            "status": raw_payload.get("status", "problem"),
            "alert_name": raw_payload.get("alert_name", "unknown alert"),
            "alert_message": raw_payload.get("message", ""),
            "tags": deepcopy(tags),
            "event_time": event_time,
        }
