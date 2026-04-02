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
        result = self._call_model_json(prompt=prompt, fallback=fallback)
        return self._normalize_judge_result(alert, context, result)

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
                "You must choose needs conservatively and deterministically.",
                "If the alert clearly matches one signal family, request the matching context family first.",
                "Do not request unrelated context blocks just because they might be interesting.",
                "Planning rules:",
                "1. Disk alerts need disk_summary, related_incidents, and alert_history.",
                "2. CPU alerts need metric_summary, related_incidents, and alert_history.",
                "3. Memory alerts need memory_summary, related_incidents, and alert_history.",
                "4. Disk IO alerts need disk_io_summary, related_incidents, and alert_history.",
                "5. Host-down alerts need availability_summary, related_incidents, and alert_history.",
                "6. If service or cluster blast radius may matter, add topology.",
                "7. Resolved alerts should still request related_incidents and alert_history.",
                "8. Request the minimum sufficient set.",
                "Need-selection hints:",
                "- disk_summary is for filesystem capacity risk, remaining space, and growth trend.",
                "- metric_summary is for CPU pressure and load correlation.",
                "- memory_summary is for memory pressure, available capacity, and swap pressure.",
                "- disk_io_summary is for storage contention, wait latency, and queue backlog.",
                "- availability_summary is for host or agent reachability.",
                "- related_incidents is for merge or suppression decisions.",
                "- alert_history is for distinguishing fresh faults from recoveries.",
                "- topology is for blast-radius and service impact decisions.",
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
                'Return JSON only with keys: {"decision":"...", "priority":"...", "reason":"...", "merge_target_incident_no":"optional", "report": {...}}',
                "Allowed decision values: notify, observe, merge, ignore.",
                "Allowed priority values: P1, P2, P3, P4.",
                "The report object must contain: summary, checks_performed, evidence, priority_rationale, recommendations.",
                "checks_performed must be a string array.",
                "evidence must be an object summarizing the key metrics you relied on.",
                "recommendations must be a string array with concrete next actions.",
                "Your response must be stable, concise, and operational.",
                "Do not hedge with long essays. Use the evidence you have.",
                "If a field is missing, do not invent it. Prefer observe when uncertainty is material.",
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
                "Priority calibration:",
                "- P1: immediate or imminent service-impact risk, likely urgent human action required.",
                "- P2: serious degradation risk, needs timely action, but not yet immediate outage.",
                "- P3: notable anomaly that should be watched or triaged soon.",
                "- P4: resolved, low-risk, or insufficiently supported for escalation.",
                "Decision calibration:",
                "- notify: send human-visible escalation now.",
                "- observe: do not ignore, but wait and continue monitoring.",
                "- merge: attach to an existing active incident.",
                "- ignore: recovery or clearly low-value signal.",
                "Type-specific judgment guide:",
                "- Disk: prioritize free_gb exhaustion risk over raw percentage when they disagree.",
                "- Disk: high used_percent plus sharp growth should increase urgency.",
                "- CPU: high cpu_max alone is weaker evidence than high cpu_avg plus high load_avg.",
                "- Memory: low available_gb or high swap pressure increases urgency sharply.",
                "- Disk IO: high queue_size with high await_ms suggests real contention, not just throughput.",
                "- Host-down: if both ping and agent are down, assume stronger outage risk.",
                "Report-writing rules:",
                "- summary should be 1-2 sentences.",
                "- checks_performed should describe the concrete validations you used.",
                "- evidence should only contain fields actually present in context or alert.",
                "- priority_rationale should explain why this priority, not just repeat the decision.",
                "- recommendations should be concrete operator actions, not generic advice.",
                "Recommendation style guide:",
                "- Prefer 3-5 short, executable actions.",
                "- Start with validation or containment, then remediation, then longer-term follow-up.",
                "- Mention the specific host, mount, metric, or symptom when useful.",
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
                "Output example skeleton:",
                json.dumps(
                    {
                        "decision": "notify",
                        "priority": "P2",
                        "reason": "Short operational reason here.",
                        "merge_target_incident_no": None,
                        "report": {
                            "summary": "One or two sentence conclusion.",
                            "checks_performed": [
                                "Checked key metric A.",
                                "Checked correlation metric B.",
                            ],
                            "evidence": {
                                "example_metric": 123
                            },
                            "priority_rationale": "Explain why this is P1/P2/P3/P4.",
                            "recommendations": [
                                "Do first operator action.",
                                "Do second operator action."
                            ]
                        }
                    },
                    ensure_ascii=False,
                ),
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

    def _normalize_judge_result(self, alert: dict, context: dict, result: dict) -> dict:
        normalized = dict(result)
        normalized.setdefault("decision", "observe")
        normalized.setdefault("priority", "P3")
        normalized.setdefault("reason", "Alert needs further observation.")
        normalized["report"] = self._normalize_report(alert, context, normalized)
        return normalized

    def _normalize_report(self, alert: dict, context: dict, result: dict) -> dict:
        report = result.get("report")
        fallback = self._build_fallback_report(alert, context, result)
        if not isinstance(report, dict):
            return fallback

        return {
            "summary": report.get("summary") or fallback["summary"],
            "checks_performed": report.get("checks_performed") or fallback["checks_performed"],
            "evidence": report.get("evidence") or fallback["evidence"],
            "priority_rationale": report.get("priority_rationale") or fallback["priority_rationale"],
            "recommendations": report.get("recommendations") or fallback["recommendations"],
        }

    def _build_fallback_report(self, alert: dict, context: dict, result: dict) -> dict:
        alert_type = alert.get("alert_type", "generic")
        evidence = {}
        checks = []
        recommendations = []

        if alert_type == "disk":
            disk = context.get("disk_summary", {})
            evidence = {
                "mount_point": disk.get("mount_point") or alert.get("resource_scope", {}).get("mount_point"),
                "used_percent": disk.get("used_percent"),
                "free_gb": disk.get("free_gb"),
                "total_gb": disk.get("total_gb"),
                "growth_gb_24h": disk.get("growth_gb_24h"),
                "trend": disk.get("trend"),
            }
            checks = [
                "Parsed disk mount point and usage threshold from alert content.",
                "Fetched disk usage, free capacity, total capacity, and growth trend from Zabbix.",
                "Checked whether there are already related open incidents.",
            ]
            recommendations = [
                "Check the largest directories and recent file growth under the affected mount.",
                "Confirm whether the recent growth is expected batch output, logs, or runaway data.",
                "Prepare cleanup or capacity expansion if free space keeps dropping.",
            ]
        elif alert_type == "cpu":
            metric = context.get("metric_summary", {})
            evidence = metric
            checks = [
                "Fetched CPU average, peak usage, and load information from Zabbix.",
                "Compared sustained CPU pressure against local and model rules.",
            ]
            recommendations = [
                "Check top CPU-consuming processes on the host.",
                "Confirm whether workload growth or stuck processes caused the spike.",
                "Consider throttling, restart, or scaling if the load remains sustained.",
            ]
        elif alert_type == "memory":
            memory = context.get("memory_summary", {})
            evidence = memory
            checks = [
                "Fetched memory utilization and available capacity from Zabbix.",
                "Checked whether memory pressure is likely to impact stability soon.",
            ]
            recommendations = [
                "Inspect top memory-consuming processes and cache growth.",
                "Check for swap activity and recent memory leak patterns.",
                "Prepare restart or scale-out if available memory keeps shrinking.",
            ]
        elif alert_type == "disk_io":
            disk_io = context.get("disk_io_summary", {})
            evidence = disk_io
            checks = [
                "Fetched disk utilization, wait latency, and queue depth from Zabbix.",
                "Checked whether the contention looks sustained or temporary.",
            ]
            recommendations = [
                "Inspect the busiest disks and processes generating IO.",
                "Check backup, compaction, or batch tasks running during the alert window.",
                "Consider workload throttling or storage optimization if latency stays high.",
            ]
        elif alert_type == "host_down":
            availability = context.get("availability_summary", {})
            evidence = availability
            checks = [
                "Checked agent availability and uptime-related host signals.",
                "Compared the host-down symptom against recent availability context.",
            ]
            recommendations = [
                "Verify network connectivity and host power state first.",
                "Check whether the Zabbix agent or firewall is blocking reachability.",
                "Escalate to infrastructure support if the host remains unreachable.",
            ]
        else:
            evidence = context
            checks = [
                "Collected available context from local systems and Zabbix.",
                "Applied generic alert evaluation rules because the alert type was uncertain.",
            ]
            recommendations = [
                "Review the raw alert text and improve alert classification if needed.",
                "Check the affected host and recent incidents for correlated symptoms.",
            ]

        return {
            "summary": result.get("reason", "Alert evaluated with available context."),
            "checks_performed": checks,
            "evidence": evidence,
            "priority_rationale": f"Decision={result.get('decision')} and priority={result.get('priority')} based on the collected evidence and alert context.",
            "recommendations": recommendations,
        }

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
