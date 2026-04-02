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
                "9. If later context contains zabbix_raw, prefer using those original items for final reasoning.",
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
                'Return JSON only with shape: {"decision":"...", "priority":"...", "reason":"...", "merge_target_incident_no":"optional", "report": {...}}',
                "Allowed decision values: notify, observe, merge, ignore.",
                "Allowed priority values: P1, P2, P3, P4.",
                "The report object must contain: summary, checks_performed, evidence, priority_rationale, recommendations.",
                "The fields reason, summary, priority_rationale, recommendations, and each element of checks_performed must be written in Simplified Chinese.",
                "Do not output markdown. Do not output any text outside JSON.",
                "Do not invent evidence. Only use fields that are actually present in the input context.",
                "If evidence is insufficient, prefer observe instead of guessing.",
                "Authoritative evidence rules:",
                "1. If context contains zabbix_raw, treat zabbix_raw as the primary source of truth for every alert type.",
                "2. Treat disk_summary, metric_summary, memory_summary, disk_io_summary, and availability_summary as convenience summaries only.",
                "3. If a convenience summary conflicts with zabbix_raw, trust zabbix_raw and explicitly mention the discrepancy in Chinese.",
                "4. In report.evidence, include the raw Zabbix items or the most relevant subset when zabbix_raw is available.",
                "5. If zabbix_raw is unavailable, use the available summaries and say evidence is limited.",
                "Decision rules:",
                "1. If related_incidents already contains a matching open incident, prefer merge.",
                "2. If alert status is resolved, prefer ignore unless it should merge into an existing incident.",
                "3. For disk alerts, evaluate mount usage, remaining capacity, total capacity, recent growth, and raw filesystem items together.",
                "4. For CPU alerts, evaluate cpu_avg, cpu_max, load_avg, and raw CPU/load items together.",
                "5. For memory alerts, evaluate memory_used_percent, available_gb, swap_used_percent, trend, and raw memory items together.",
                "6. For disk IO alerts, evaluate utilization_percent, await_ms, queue_size, trend, and raw storage items together.",
                "7. For host-down alerts, evaluate ping_status, agent_status, last_seen_minutes_ago, and raw availability items together.",
                "8. For the same technical severity, production environments should be prioritized above non-production environments.",
                "Priority definitions:",
                "- P1: Immediate or near-immediate business risk, urgent human action required.",
                "- P2: High risk that should be handled soon, but not yet an immediate outage.",
                "- P3: Needs observation or troubleshooting, but evidence does not support urgent escalation.",
                "- P4: Recovered, low-value, duplicate, or insufficient evidence to escalate.",
                "Decision definitions:",
                "- notify: Human notification is required now.",
                "- observe: Keep watching, do not escalate yet.",
                "- merge: Merge into an existing incident.",
                "- ignore: No further action is needed now.",
                "Alert-specific heuristics:",
                "- Disk: low free space can outweigh a slightly lower used_percent.",
                "- Disk: rapid growth increases urgency even if current percentage is borderline.",
                "- CPU: sustained high average is usually more serious than a short spike.",
                "- Memory: low available memory or heavy swap pressure increases urgency.",
                "- Disk IO: high queue_size plus high await_ms indicates real storage contention.",
                "- Host down: if both ping and agent appear down, treat as higher risk.",
                "Report writing rules:",
                "- summary: 1 to 2 concise Chinese sentences.",
                "- checks_performed: Chinese list of what you actually checked.",
                "- priority_rationale: explain in Chinese why this priority is appropriate.",
                "- recommendations: 3 to 5 actionable Chinese suggestions, starting with validation/containment and then remediation.",
                "Alert input:",
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
                "Context input:",
                json.dumps(context, ensure_ascii=False),
                "Output example:",
                json.dumps(
                    {
                        "decision": "notify",
                        "priority": "P2",
                        "reason": "这里写简短中文原因。",
                        "merge_target_incident_no": None,
                        "report": {
                            "summary": "这里写 1 到 2 句中文总结。",
                            "checks_performed": [
                                "检查了关键指标 A。",
                                "检查了关联指标 B。",
                            ],
                            "evidence": {
                                "example_metric": 123,
                            },
                            "priority_rationale": "这里解释为什么是这个优先级。",
                            "recommendations": [
                                "这里写第一条处理建议。",
                                "这里写第二条处理建议。",
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
            ]
        )

    @staticmethod
    def _extract_json_object(text: str) -> dict:
        decoder = json.JSONDecoder()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{", text)
            if match:
                return decoder.raw_decode(text[match.start():])[0]
            raise

    def _normalize_judge_result(self, alert: dict, context: dict, result: dict) -> dict:
        normalized = dict(result)
        normalized.setdefault("decision", "observe")
        normalized.setdefault("priority", "P3")
        normalized.setdefault("reason", "告警需要继续观察。")
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
                "zabbix_raw": context.get("zabbix_raw"),
            }
            checks = [
                "从告警内容中解析了挂载点和阈值。",
                "获取了磁盘使用率、剩余空间、总容量和趋势信息。",
                "检查了是否存在相关的未关闭 incident。",
            ]
            recommendations = [
                "检查受影响挂载点下占用空间最大的目录和最近增长最快的文件。",
                "确认增长是否来自批处理输出、日志膨胀或异常数据写入。",
                "如果剩余空间继续下降，尽快执行清理或扩容。",
            ]
        elif alert_type == "cpu":
            metric = context.get("metric_summary", {})
            evidence = {
                **metric,
                "zabbix_raw": context.get("zabbix_raw"),
            }
            checks = [
                "获取了 CPU 平均值、峰值和负载信息。",
                "对比了持续高负载和瞬时尖峰的差异。",
            ]
            recommendations = [
                "检查主机上 CPU 占用最高的进程。",
                "确认是业务流量增长还是异常进程导致的 CPU 升高。",
                "如果高负载持续存在，考虑限流、重启或扩容。",
            ]
        elif alert_type == "memory":
            memory = context.get("memory_summary", {})
            evidence = {
                **memory,
                "zabbix_raw": context.get("zabbix_raw"),
            }
            checks = [
                "获取了内存使用率和可用容量。",
                "检查了内存压力是否已经接近影响稳定性的阈值。",
            ]
            recommendations = [
                "检查最占内存的进程和缓存增长情况。",
                "确认是否存在 swap 压力和疑似内存泄漏。",
                "如果可用内存继续下降，准备重启或扩容。",
            ]
        elif alert_type == "disk_io":
            disk_io = context.get("disk_io_summary", {})
            evidence = {
                **disk_io,
                "zabbix_raw": context.get("zabbix_raw"),
            }
            checks = [
                "获取了磁盘利用率、等待时间和队列长度。",
                "检查了磁盘争用是持续存在还是短时波动。",
            ]
            recommendations = [
                "检查最繁忙的磁盘和产生 IO 的关键进程。",
                "确认告警窗口内是否有备份、压缩或批处理任务。",
                "如果延迟持续偏高，考虑限流或存储优化。",
            ]
        elif alert_type == "host_down":
            availability = context.get("availability_summary", {})
            evidence = {
                **availability,
                "zabbix_raw": context.get("zabbix_raw"),
            }
            checks = [
                "检查了 agent 可用性和主机运行状态相关信号。",
                "结合最近可用性上下文判断是否为真实宕机。",
            ]
            recommendations = [
                "先确认网络连通性和主机电源状态。",
                "检查 Zabbix agent 或防火墙是否导致不可达。",
                "如果主机持续不可达，升级给基础设施支持处理。",
            ]
        else:
            evidence = {
                "context": context,
                "zabbix_raw": context.get("zabbix_raw"),
            }
            checks = [
                "收集了当前可获取的上下文信息。",
                "由于告警类型不够明确，按通用规则进行了评估。",
            ]
            recommendations = [
                "复核原始告警文本，必要时补强告警分类规则。",
                "检查受影响主机和近期事件，确认是否存在关联症状。",
            ]

        return {
            "summary": result.get("reason", "已基于当前可用上下文完成告警评估。"),
            "checks_performed": checks,
            "evidence": evidence,
            "priority_rationale": f"根据当前证据和告警上下文，最终决策为 {result.get('decision')}，优先级为 {result.get('priority')}。",
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
                "reason": "告警已恢复，当前无需升级处理。",
            }

        related = context.get("related_incidents", [])
        if related:
            return {
                "decision": "merge",
                "priority": "P2",
                "reason": "发现同主机或同服务的未关闭 incident，建议并入已有事件。",
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
            "reason": "当前更适合先观察，再决定是否升级。",
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
                "reason": "磁盘使用率已经很高或剩余空间接近耗尽，需要尽快处理。",
            }
        if used_percent >= 93 and growth >= 20:
            return {
                "decision": "notify",
                "priority": "P2",
                "reason": "磁盘使用率偏高且近期增长很快，存在快速写满风险。",
            }
        return {
            "decision": "observe",
            "priority": "P3",
            "reason": "磁盘在缓慢增长，建议持续观察。",
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
                "reason": "CPU 持续高位运行且负载偏高，需要及时处理。",
            }
        if cpu_max >= 90:
            return {
                "decision": "observe",
                "priority": "P3",
                "reason": "CPU 出现高峰，但当前更适合先观察是否持续。",
            }
        return {
            "decision": "ignore",
            "priority": "P4",
            "reason": "当前 CPU 上下文不足以支持升级处理。",
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
                "reason": "内存压力较大，可能很快影响服务稳定性。",
            }
        if used_percent >= 90:
            return {
                "decision": "observe",
                "priority": "P3",
                "reason": "内存使用率较高，但暂未证明即将耗尽，建议观察。",
            }
        return {
            "decision": "ignore",
            "priority": "P4",
            "reason": "当前内存上下文不足以支持升级处理。",
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
                "reason": "磁盘 IO 饱和且等待时间较高，需要尽快处理。",
            }
        if utilization >= 85 or queue_size >= 3:
            return {
                "decision": "observe",
                "priority": "P3",
                "reason": "磁盘 IO 偏高，建议观察是否持续恶化。",
            }
        return {
            "decision": "ignore",
            "priority": "P4",
            "reason": "当前磁盘 IO 上下文未显示严重争用。",
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
                "reason": "主机从 ping 和 agent 两个维度看都不可用，需要尽快处理。",
            }
        return {
            "decision": "observe",
            "priority": "P3",
            "reason": "可用性信号存在分歧，建议继续观察。",
        }
