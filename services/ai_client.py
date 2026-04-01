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
            "prompt": self._build_plan_prompt(alert),
            "alert": {
                "alert_name": alert["alert_name"],
                "host_name": alert["host_name"],
                "host_ip": alert["host_ip"],
                "severity": alert["severity"],
                "status": alert["status"],
                "tags": alert["tags"],
                "alert_type": alert.get("alert_type"),
                "resource_scope": alert.get("resource_scope", {}),
                "signal": alert.get("signal", {}),
            }
        }
        return self._post_json("/plan_context", payload, fallback=self._stub_plan(alert))

    def judge_alert(self, alert: dict, context: dict) -> dict:
        payload = {
            "prompt": self._build_judge_prompt(alert, context),
            "alert": {
                "alert_name": alert["alert_name"],
                "host_name": alert["host_name"],
                "host_ip": alert["host_ip"],
                "severity": alert["severity"],
                "status": alert["status"],
                "tags": alert["tags"],
                "alert_type": alert.get("alert_type"),
                "resource_scope": alert.get("resource_scope", {}),
                "signal": alert.get("signal", {}),
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

    def _build_plan_prompt(self, alert: dict) -> str:
        return "\n".join(
            [
                "You are an alert-context planner for infrastructure incidents.",
                "Your task is to decide which local context blocks are required before final judgment.",
                "Return JSON only with shape: {\"needs\": [...]}",
                "Allowed needs: metric_summary, disk_summary, topology, related_incidents, alert_history",
                "Do not give explanations, markdown, or extra keys.",
                "Planning rules:",
                "1. If alert_type is disk, request disk_summary, related_incidents, and alert_history.",
                "2. If alert_type is cpu, request metric_summary, related_incidents, and alert_history.",
                "3. If service or topology may affect blast radius, request topology.",
                "4. For resolved alerts, still request related_incidents and alert_history so downstream can decide closure or suppression.",
                "5. Prefer the smallest sufficient need set.",
                f"Current alert_type: {alert.get('alert_type', 'generic')}",
                f"Current status: {alert.get('status', 'problem')}",
                f"Current resource_scope: {alert.get('resource_scope', {})}",
                f"Current signal: {alert.get('signal', {})}",
            ]
        )

    def _build_judge_prompt(self, alert: dict, context: dict) -> str:
        return "\n".join(
            [
                "You are an infrastructure alert judge.",
                "Return JSON only with keys: decision, priority, reason, merge_target_incident_no (optional).",
                "Allowed decision values: notify, observe, merge, ignore.",
                "Allowed priority values: P1, P2, P3, P4.",
                "Reason must be concise and action-oriented.",
                "Judgment rules:",
                "1. If related_incidents already contains a matching open incident, prefer merge.",
                "2. If alert status is resolved, prefer ignore unless it clearly updates an open incident.",
                "3. Disk alerts must consider used_percent, free_gb, total_gb, and growth_gb_24h.",
                "4. For disk alerts, rapid consumption is more urgent than slow consumption at the same percentage.",
                "5. CPU alerts must consider cpu_avg, cpu_max, and load_avg together.",
                "6. Production environment should raise priority by one level compared with non-prod when impact is equivalent.",
                "7. If evidence is incomplete, use observe instead of over-escalating.",
                "8. Ignore only when context clearly shows low risk or recovery.",
                f"Current alert_type: {alert.get('alert_type', 'generic')}",
                f"Current status: {alert.get('status', 'problem')}",
                f"Current tags: {alert.get('tags', {})}",
                f"Current resource_scope: {alert.get('resource_scope', {})}",
                f"Current signal: {alert.get('signal', {})}",
                f"Context keys: {list(context.keys())}",
            ]
        )

    @staticmethod
    def _stub_plan(alert: dict) -> dict:
        service = alert.get("tags", {}).get("service")
        alert_type = alert.get("alert_type", "generic")

        if alert_type == "disk":
            needs = ["disk_summary", "related_incidents", "alert_history"]
        elif alert_type == "cpu":
            needs = ["metric_summary", "related_incidents", "alert_history"]
        else:
            needs = ["metric_summary", "related_incidents"]

        if service:
            needs.append("topology")

        return {"needs": list(dict.fromkeys(needs))}

    @staticmethod
    def _stub_judge(alert: dict, context: dict) -> dict:
        if alert.get("status") == "resolved":
            return {
                "decision": "ignore",
                "priority": "P4",
                "reason": "Resolved alert does not need escalation.",
            }

        related = context.get("related_incidents", [])
        if related:
            return {
                "decision": "merge",
                "priority": "P2",
                "reason": "Found open incident for same host or service.",
                "merge_target_incident_no": related[0]["incident_no"],
            }

        alert_type = alert.get("alert_type", "generic")
        if alert_type == "disk":
            return AIClient._judge_disk(alert, context)
        if alert_type == "cpu":
            return AIClient._judge_cpu(alert, context)

        return {
            "decision": "observe",
            "priority": "P3",
            "reason": "Alert should be observed before escalation.",
        }

    @staticmethod
    def _judge_disk(alert: dict, context: dict) -> dict:
        disk = context.get("disk_summary", {})
        env = alert.get("tags", {}).get("env", "").lower()
        used_percent = float(disk.get("used_percent", 0))
        free_gb = float(disk.get("free_gb", 0))
        growth = float(disk.get("growth_gb_24h", 0))

        if used_percent >= 97 or free_gb <= 10:
            return {
                "decision": "notify",
                "priority": "P1" if env == "prod" else "P2",
                "reason": "Disk usage is critically high or free space is nearly exhausted.",
            }

        if used_percent >= 93 and growth >= 20:
            return {
                "decision": "notify",
                "priority": "P2",
                "reason": "Disk usage is high and recent growth indicates rapid consumption.",
            }

        return {
            "decision": "observe",
            "priority": "P3",
            "reason": "Disk is filling gradually and should be monitored.",
        }

    @staticmethod
    def _judge_cpu(alert: dict, context: dict) -> dict:
        env = alert.get("tags", {}).get("env", "").lower()
        metric_summary = context.get("metric_summary", {})
        cpu_max = float(metric_summary.get("cpu_max", 0))
        cpu_avg = float(metric_summary.get("cpu_avg", 0))
        load_avg = float(metric_summary.get("load_avg", 0))

        if cpu_max >= 95 and cpu_avg >= 90 and load_avg >= 8:
            return {
                "decision": "notify",
                "priority": "P1" if env == "prod" else "P2",
                "reason": "CPU is sustained at a very high level with elevated load.",
            }

        if cpu_max >= 90:
            return {
                "decision": "observe",
                "priority": "P3",
                "reason": "CPU spike needs observation but is not yet severe enough for paging.",
            }

        return {
            "decision": "ignore",
            "priority": "P4",
            "reason": "Current CPU context does not support escalation.",
        }
