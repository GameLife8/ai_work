from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, datetime


class AlertParser:
    @staticmethod
    def parse(raw_payload: dict) -> dict:
        tags = raw_payload.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}

        alert_name = AlertParser._coalesce(
            raw_payload.get("alert_message"),
            raw_payload.get("alert_name"),
            raw_payload.get("alert_level"),
            raw_payload.get("alert_title"),
            raw_payload.get("subject"),
            raw_payload.get("title"),
            "unknown alert",
        )
        alert_message = AlertParser._coalesce(
            raw_payload.get("alert_detail"),
            raw_payload.get("message"),
            raw_payload.get("content"),
            raw_payload.get("description"),
            "",
        )
        host_name = str(raw_payload.get("host_name", "")).strip()
        host_ip = str(raw_payload.get("host_ip", "")).strip()

        inferred_host_name, inferred_host_ip = AlertParser._extract_host_from_text(alert_message)
        host_name = host_name or inferred_host_name
        host_ip = host_ip or inferred_host_ip

        status = str(raw_payload.get("status", "")).strip().lower() or AlertParser._infer_status(alert_message)
        severity = str(raw_payload.get("severity", "")).strip().lower()
        event_time = raw_payload.get("event_time") or raw_payload.get("created_at") or datetime.now(UTC).isoformat()

        normalized = {
            "source": "zabbix",
            "source_event_id": str(raw_payload.get("event_id", "")),
            "source_problem_id": str(raw_payload.get("problem_id", "")),
            "trigger_id": str(raw_payload.get("trigger_id", "")),
            "host_id": str(raw_payload.get("host_id", "")),
            "host_name": host_name,
            "host_ip": host_ip,
            "severity": severity or AlertParser._infer_severity(alert_name, alert_message),
            "status": status or "problem",
            "alert_name": alert_name,
            "alert_message": alert_message,
            "tags": deepcopy(tags),
            "event_time": event_time,
            "notify_target": str(raw_payload.get("to_user", "")),
            "source_subject": str(raw_payload.get("subject", "")),
        }
        normalized.update(AlertParser._extract_signal_fields(normalized))
        return normalized

    @staticmethod
    def _coalesce(*values: object) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _extract_host_from_text(text: str) -> tuple[str, str]:
        if not text:
            return "", ""
        match = re.search(r"([A-Za-z0-9._-]+)\s*\((\d{1,3}(?:\.\d{1,3}){3})\)", text)
        if match:
            return match.group(1), match.group(2)
        return "", ""

    @staticmethod
    def _infer_status(text: str) -> str:
        lowered = text.lower()
        if "resolved" in lowered:
            return "resolved"
        if "problem" in lowered:
            return "problem"
        return "problem"

    @staticmethod
    def _infer_severity(alert_name: str, alert_message: str) -> str:
        text = f"{alert_name} {alert_message}".lower()
        if any(token in text for token in ["down", "unreachable", "not available", "> 95%", ">95%"]):
            return "critical"
        if any(token in text for token in ["high", "> 90%", ">90%", "over 90%"]):
            return "high"
        return "warning"

    @staticmethod
    def _extract_signal_fields(alert: dict) -> dict:
        text = f"{alert['alert_name']} {alert['alert_message']}"
        lowered = text.lower()
        signal = {"alert_type": "generic", "resource_scope": {}, "signal": {}}

        mount = AlertParser._extract_mount_point(text)
        threshold = AlertParser._extract_threshold_percent(text)
        duration_minutes = AlertParser._extract_duration_minutes(lowered)

        is_disk = bool(mount and threshold is not None and "used" in lowered)
        is_cpu = duration_minutes is not None and threshold is not None
        is_memory = threshold is not None and ("memory" in lowered or "内存" in text)
        is_disk_io = any(token in lowered for token in ["disk io", "i/o", "iowait", "avg. disk", "queue length"])
        is_host_down = any(token in lowered for token in ["agent is not available", "unreachable", "icmp ping", "is down"])

        if is_disk:
            signal["alert_type"] = "disk"
            signal["resource_scope"] = {"mount_point": mount}
            signal["signal"] = {
                "threshold_percent": threshold,
                "symptom": "disk_used_percent_high",
            }
            return signal

        if is_cpu:
            signal["alert_type"] = "cpu"
            signal["signal"] = {
                "threshold_percent": threshold,
                "duration_minutes": duration_minutes,
                "symptom": "cpu_used_percent_high",
            }
            return signal

        if is_memory:
            signal["alert_type"] = "memory"
            signal["signal"] = {
                "threshold_percent": threshold,
                "symptom": "memory_used_percent_high",
            }
            return signal

        if is_disk_io:
            signal["alert_type"] = "disk_io"
            if mount:
                signal["resource_scope"] = {"mount_point": mount}
            signal["signal"] = {"symptom": "disk_io_high"}
            return signal

        if is_host_down:
            signal["alert_type"] = "host_down"
            signal["signal"] = {"symptom": "host_unreachable"}
            return signal

        if mount and threshold is not None:
            signal["alert_type"] = "disk"
            signal["resource_scope"] = {"mount_point": mount}
            signal["signal"] = {
                "threshold_percent": threshold,
                "symptom": "disk_used_percent_high",
            }
            return signal

        if "cpu" in lowered:
            signal["alert_type"] = "cpu"
            return signal

        return signal

    @staticmethod
    def _extract_mount_point(text: str) -> str | None:
        match = re.search(r"([A-Za-z]:|/[A-Za-z0-9._/-]+)\s*[:：]?", text)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_threshold_percent(text: str) -> int | None:
        match = re.search(r"(?:>\s*|over\s+)(\d+)%", text.lower())
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _extract_duration_minutes(text: str) -> int | None:
        match = re.search(r"for\s*(\d+)m", text)
        if match:
            return int(match.group(1))
        return None
