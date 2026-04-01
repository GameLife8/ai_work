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
        if "resolved" in lowered or "恢复" in text:
            return "resolved"
        if "problem" in lowered or "严重不足" in text or "高 cpu" in lowered:
            return "problem"
        return "problem"

    @staticmethod
    def _infer_severity(alert_name: str, alert_message: str) -> str:
        text = f"{alert_name} {alert_message}".lower()
        if any(token in text for token in ["关机", "down", "unreachable", "not available"]):
            return "critical"
        if any(token in text for token in ["critical", "严重", "> 95%", ">95%"]):
            return "critical"
        if any(token in text for token in ["high", "严重不足", "> 90%", ">90%"]):
            return "high"
        return "warning"

    @staticmethod
    def _extract_signal_fields(alert: dict) -> dict:
        text = f"{alert['alert_name']} {alert['alert_message']}"
        lowered = text.lower()

        signal = {"alert_type": "generic", "resource_scope": {}, "signal": {}}

        disk_match = re.search(r"(?P<mount>/[A-Za-z0-9._/-]+)\s*[:：].*?(?:磁盘空间|disk).*?used\s*>\s*(?P<threshold>\d+)%", text)
        if disk_match:
            signal["alert_type"] = "disk"
            signal["resource_scope"] = {"mount_point": disk_match.group("mount")}
            signal["signal"] = {
                "threshold_percent": int(disk_match.group("threshold")),
                "symptom": "disk_used_percent_high",
            }
            return signal

        cpu_match = re.search(r"over\s*(?P<threshold>\d+)%\s*for\s*(?P<minutes>\d+)m", lowered)
        if cpu_match:
            signal["alert_type"] = "cpu"
            signal["signal"] = {
                "threshold_percent": int(cpu_match.group("threshold")),
                "duration_minutes": int(cpu_match.group("minutes")),
                "symptom": "cpu_used_percent_high",
            }
            return signal

        memory_match = re.search(r"(?:内存|memory).*?(?:used|usage|利用率)?\s*>\s*(?P<threshold>\d+)%", lowered)
        if memory_match or "内存" in text or "memory" in lowered:
            signal["alert_type"] = "memory"
            signal["signal"] = {
                "threshold_percent": int(memory_match.group("threshold")) if memory_match else 90,
                "symptom": "memory_used_percent_high",
            }
            return signal

        if any(token in lowered for token in ["磁盘吞吐", "disk io", "i/o", "iowait", "avg. disk", "queue length"]):
            signal["alert_type"] = "disk_io"
            mount_match = re.search(r"([A-Za-z]:|/[A-Za-z0-9._/-]+)", text)
            if mount_match:
                signal["resource_scope"] = {"mount_point": mount_match.group(1)}
            signal["signal"] = {"symptom": "disk_io_high"}
            return signal

        if any(token in lowered for token in ["agent is not available", "unreachable", "icmp ping", "is down"]) or "关机" in text:
            signal["alert_type"] = "host_down"
            signal["signal"] = {"symptom": "host_unreachable"}
            return signal

        if "磁盘空间" in text:
            signal["alert_type"] = "disk"
            mount_match = re.search(r"(?P<mount>/[A-Za-z0-9._/-]+)", text)
            if mount_match:
                signal["resource_scope"] = {"mount_point": mount_match.group("mount")}
            return signal

        if "cpu" in lowered:
            signal["alert_type"] = "cpu"
            return signal

        return signal
