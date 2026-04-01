from __future__ import annotations

import logging

import requests


logger = logging.getLogger(__name__)


class AIClient:
    def __init__(self, base_url: str, timeout_seconds: int, use_stub: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.use_stub = use_stub

    def plan_context(self, alert: dict) -> dict:
        payload = {
            "alert": {
                "alert_name": alert["alert_name"],
                "host_name": alert["host_name"],
                "host_ip": alert["host_ip"],
                "severity": alert["severity"],
                "tags": alert["tags"],
            }
        }
        return self._post_json("/plan_context", payload, fallback=self._stub_plan(alert))

    def judge_alert(self, alert: dict, context: dict) -> dict:
        payload = {
            "alert": {
                "alert_name": alert["alert_name"],
                "host_name": alert["host_name"],
                "host_ip": alert["host_ip"],
                "severity": alert["severity"],
                "tags": alert["tags"],
            },
            "context": context,
        }
        return self._post_json("/judge_alert", payload, fallback=self._stub_judge(alert, context))

    def _post_json(self, path: str, payload: dict, fallback: dict) -> dict:
        if self.use_stub:
            return fallback

        url = f"{self.base_url}{path}"
        try:
            response = requests.post(url, json=payload, timeout=self.timeout_seconds)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
        except Exception as exc:  # pragma: no cover
            logger.warning("AI call failed, falling back to local rule: %s", exc)
        return fallback

    @staticmethod
    def _stub_plan(alert: dict) -> dict:
        service = alert.get("tags", {}).get("service")
        needs = ["metric_summary", "related_incidents"]
        if service:
            needs.append("topology")
        return {"needs": needs}

    @staticmethod
    def _stub_judge(alert: dict, context: dict) -> dict:
        env = alert.get("tags", {}).get("env", "").lower()
        severity = alert.get("severity", "").lower()
        metric_summary = context.get("metric_summary", {})
        cpu_max = metric_summary.get("cpu_max", 0)
        related = context.get("related_incidents", [])

        if related:
            return {
                "decision": "merge",
                "priority": "P2",
                "reason": "Found open incident for same host or service.",
                "merge_target_incident_no": related[0]["incident_no"],
            }

        if severity in {"high", "critical"} and env == "prod" and cpu_max >= 90:
            return {
                "decision": "notify",
                "priority": "P1",
                "reason": "High severity production alert with sustained high CPU.",
            }

        return {
            "decision": "observe",
            "priority": "P3",
            "reason": "Alert should be observed before escalation.",
        }
