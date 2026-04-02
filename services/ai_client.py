from __future__ import annotations

import json
import logging
import re

import requests


logger = logging.getLogger(__name__)


class AIClient:
    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int,
        use_stub: bool = True,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.use_stub = use_stub

    def plan_context(self, alert: dict) -> dict:
        prompt = self._build_plan_prompt(alert)
        fallback = self._stub_plan(alert)
        return self._call_model_json(prompt=prompt, fallback=fallback)

    def judge_alert(self, alert: dict, context: dict) -> dict:
        prompt = self._build_judge_prompt(alert, context)
        fallback = self._stub_judge(alert, context)
        return self._call_model_json(prompt=prompt, fallback=fallback)

    def _call_model_json(self, prompt: str, fallback: dict) -> dict:
        if self.use_stub:
            return fallback
        if self.provider != "volcengine_coding":
            return self._call_legacy_json(prompt, fallback)
        if not self.api_key or not self.model:
            logger.warning("AI API key or model is missing, falling back to local rule.")
            return fallback

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a strict JSON-only infrastructure alert assistant.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "temperature": 0.1,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]["content"]
            parsed = self._extract_json_object(message)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:  # pragma: no cover
            logger.warning("Volcengine AI call failed, falling back to local rule: %s", exc)
        return fallback

    def _call_legacy_json(self, prompt: str, fallback: dict) -> dict:
        try:
            response = requests.post(
                self.base_url,
                json={"prompt": prompt},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
        except Exception as exc:  # pragma: no cover
            logger.warning("Legacy AI call failed, falling back to local rule: %s", exc)
        return fallback

    def _build_plan_prompt(self, alert: dict) -> str:
        return "\n".join(
            [
                "You are an alert-context planner for infrastructure incidents.",
                'Return JSON only with shape: {"needs": [...]}',
                "Allowed needs: metric_summary, disk_summary, memory_summary, disk_io_summary, availability_summary, topology, related_incidents, alert_history",
                "Do not output markdown or extra keys.",
                "Planning rules:",
                "1. Disk alerts need disk_summary, related_incidents, and alert_history.",
                "2. CPU alerts need metric_summary, related_incidents, and alert_history.",
                "3. Memory alerts need memory_summary, related_incidents, and alert_history.",
                "4. Disk IO alerts need disk_io_summary, related_incidents, and alert_history.",
                "5. Host-down alerts need availability_summary, related_incidents, and alert_history.",
                "6. If service or cluster blast radius may matter, add topology.",
                "7. Resolved alerts should still request related_incidents and alert_history.",
                "8. Request the minimum sufficient set.",
                "Alert payload:",
                json.dumps(
                    {
                        "alert_name": alert.get("alert_name"),
                        "host_name": alert.get("host_name"),
                        "host_ip": alert.get("host_ip"),
                        "severity": alert.get("severity"),
                        "status": alert.get("status"),
                        "tags": alert.get("tags", {}),
                        "alert_type": alert.get("alert_type"),
                        "resource_scope": alert.get("resource_scope", {}),
                        "signal": alert.get("signal", {}),
                    },
                    ensure_ascii=False,
                ),
            ]
        )

    def _build_judge_prompt(self, alert: dict, context: dict) -> str:
        return "\n".join(
            [
                "You are an infrastructure alert judge.",
                'Return JSON only with keys: {"decision":"...", "priority":"...", "reason":"...", "merge_target_incident_no":"optional"}',
                "Allowed decision values: notify, observe, merge, ignore.",
                "Allowed priority values: P1, P2, P3, P4.",
                "Decision rules:",
                "1. If related_incidents already contains a matching open incident, prefer merge.",
                "2. If alert status is resolved, prefer ignore unless it should merge into an open incident.",
                "3. Disk alerts must consider used_percent, free_gb, total_gb, growth_gb_24h, and trend.",
                "4. Disk alerts with very low remaining free space or very rapid growth are urgent.",
                "5. CPU alerts must consider cpu_avg, cpu_max, and load_avg together.",
                "6. Memory alerts must consider memory_used_percent, available_gb, swap_used_percent, and trend.",
                "7. Disk IO alerts must consider utilization_percent, await_ms, queue_size, and trend.",
                "8. Host-down alerts must consider ping_status, agent_status, and last_seen_minutes_ago.",
                "9. Production equivalent impact should have higher priority than non-prod.",
                "10. If evidence is incomplete, prefer observe rather than over-escalating.",
                "11. Keep reason concise, concrete, and operational.",
                "Alert payload:",
                json.dumps(
                    {
                        "alert_name": alert.get("alert_name"),
                        "host_name": alert.get("host_name"),
                        "host_ip": alert.get("host_ip"),
                        "severity": alert.get("severity"),
                        "status": alert.get("status"),
                        "tags": alert.get("tags", {}),
                        "alert_type": alert.get("alert_type"),
                        "resource_scope": alert.get("resource_scope", {}),
                        "signal": alert.get("signal", {}),
                    },
                    ensure_ascii=False,
                ),
                "Context payload:",
                json.dumps(context, ensure_ascii=False),
            ]
        )

    @staticmethod
    def _extract_json_object(text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

    @staticmethod
    def _stub_plan(alert: dict) -> dict:
        service = alert.get("tags", {}).get("service")
        alert_type = alert.get("alert_type", "generic")

        needs_map = {
            "disk": ["disk_summary", "related_incidents", "alert_history"],
            "cpu": ["metric_summary", "related_incidents", "alert_history"],
            "memory": ["memory_summary", "related_incidents", "alert_history"],
            "disk_io": ["disk_io_summary", "related_incidents", "alert_history"],
            "host_down": ["availability_summary", "related_incidents", "alert_history"],
        }
        needs = needs_map.get(alert_type, ["metric_summary", "related_incidents"])

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
        if alert_type == "memory":
            return AIClient._judge_memory(alert, context)
        if alert_type == "disk_io":
            return AIClient._judge_disk_io(alert, context)
        if alert_type == "host_down":
            return AIClient._judge_host_down(alert, context)

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

    @staticmethod
    def _judge_memory(alert: dict, context: dict) -> dict:
        env = alert.get("tags", {}).get("env", "").lower()
        summary = context.get("memory_summary", {})
        used_percent = float(summary.get("memory_used_percent", 0))
        available_gb = float(summary.get("available_gb", 0))
        swap_used_percent = float(summary.get("swap_used_percent", 0))

        if used_percent >= 95 or available_gb <= 2 or swap_used_percent >= 60:
            return {
                "decision": "notify",
                "priority": "P1" if env == "prod" else "P2",
                "reason": "Memory pressure is severe and may soon cause service instability.",
            }
        if used_percent >= 90:
            return {
                "decision": "observe",
                "priority": "P3",
                "reason": "Memory usage is high but immediate exhaustion risk is not yet proven.",
            }
        return {
            "decision": "ignore",
            "priority": "P4",
            "reason": "Current memory context does not support escalation.",
        }

    @staticmethod
    def _judge_disk_io(alert: dict, context: dict) -> dict:
        env = alert.get("tags", {}).get("env", "").lower()
        summary = context.get("disk_io_summary", {})
        utilization = float(summary.get("utilization_percent", 0))
        await_ms = float(summary.get("await_ms", 0))
        queue_size = float(summary.get("queue_size", 0))

        if utilization >= 95 and await_ms >= 50:
            return {
                "decision": "notify",
                "priority": "P1" if env == "prod" else "P2",
                "reason": "Disk IO is saturated with high wait latency.",
            }
        if utilization >= 85 or queue_size >= 3:
            return {
                "decision": "observe",
                "priority": "P3",
                "reason": "Disk IO is elevated and should be watched for sustained degradation.",
            }
        return {
            "decision": "ignore",
            "priority": "P4",
            "reason": "Disk IO context does not show critical contention.",
        }

    @staticmethod
    def _judge_host_down(alert: dict, context: dict) -> dict:
        env = alert.get("tags", {}).get("env", "").lower()
        summary = context.get("availability_summary", {})
        ping = summary.get("ping_status", "down")
        agent = summary.get("agent_status", "down")

        if ping == "down" and agent == "down":
            return {
                "decision": "notify",
                "priority": "P1" if env == "prod" else "P2",
                "reason": "Host appears unavailable from both ping and agent perspectives.",
            }
        return {
            "decision": "observe",
            "priority": "P3",
            "reason": "Availability signals are mixed and need observation.",
        }
