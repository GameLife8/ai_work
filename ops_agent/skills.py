from __future__ import annotations

import json
from typing import Any


class UnifiedSkillRegistry:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.tools = self._build_tools()
        self.handlers = {
            "swarm_list_services": self.swarm_list_services,
            "swarm_get_service_detail": self.swarm_get_service_detail,
            "swarm_get_service_status": self.swarm_get_service_status,
            "swarm_get_failed_tasks": self.swarm_get_failed_tasks,
            "swarm_check_service_health": self.swarm_check_service_health,
            "swarm_get_service_logs_filter": self.swarm_get_service_logs_filter,
            "zabbix_get_host_storage_overview": self.zabbix_get_host_storage_overview,
            "zabbix_get_host_overview": self.zabbix_get_host_overview,
            "alerts_analyze_payload": self.alerts_analyze_payload,
        }

    def execute(self, tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers[tool_name]
        return handler(**tool_args)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in self.tools
        ]

    def _build_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "swarm_list_services",
                "description": "列出 Docker Swarm 服务，适合先确认服务是否存在、当前副本是否正常。",
                "parameters": {
                    "type": "object",
                    "properties": {"filter_name": {"type": "string"}},
                },
            },
            {
                "name": "swarm_get_service_detail",
                "description": "查看服务详情，包括镜像、环境变量、更新状态、约束和网络。",
                "parameters": {
                    "type": "object",
                    "properties": {"service_name": {"type": "string"}},
                    "required": ["service_name"],
                },
            },
            {
                "name": "swarm_get_service_status",
                "description": "查看服务当前状态和副本情况。",
                "parameters": {
                    "type": "object",
                    "properties": {"service_name": {"type": "string"}},
                    "required": ["service_name"],
                },
            },
            {
                "name": "swarm_get_failed_tasks",
                "description": "查看服务最近失败任务和错误原因，适合定位起不来、不断重启、更新失败。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["service_name"],
                },
            },
            {
                "name": "swarm_check_service_health",
                "description": "汇总服务健康状态、更新状态、重启策略和失败任务。",
                "parameters": {
                    "type": "object",
                    "properties": {"service_name": {"type": "string"}},
                    "required": ["service_name"],
                },
            },
            {
                "name": "swarm_get_service_logs_filter",
                "description": "按关键词过滤服务日志，适合查询 error、exception、timeout 等排障证据。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string"},
                        "keyword": {"type": "string"},
                        "tail": {"type": "integer"},
                        "since": {"type": "string"},
                    },
                    "required": ["service_name", "keyword"],
                },
            },
            {
                "name": "zabbix_get_host_storage_overview",
                "description": "获取某台主机所有磁盘或挂载点的当前容量、剩余空间和使用率，适合回答主机硬盘情况。",
                "parameters": {
                    "type": "object",
                    "properties": {"host_query": {"type": "string"}},
                    "required": ["host_query"],
                },
            },
            {
                "name": "zabbix_get_host_overview",
                "description": "获取主机 CPU、内存、可用性等概览信息，适合用户想了解主机当前总体运行状态。",
                "parameters": {
                    "type": "object",
                    "properties": {"host_query": {"type": "string"}},
                    "required": ["host_query"],
                },
            },
            {
                "name": "alerts_analyze_payload",
                "description": "对一段原始告警 payload 进行预分析，返回告警解析、上下文规划、补数结果和模型决策，但不落库。",
                "parameters": {
                    "type": "object",
                    "properties": {"raw_payload_json": {"type": "string"}},
                    "required": ["raw_payload_json"],
                },
            },
        ]

    def swarm_list_services(self, filter_name: str | None = None) -> dict:
        return self.runtime.docker_swarm_client.list_services(filter_name)

    def swarm_get_service_detail(self, service_name: str) -> dict:
        return self.runtime.docker_swarm_client.get_service_detail(service_name)

    def swarm_get_service_status(self, service_name: str) -> dict:
        return self.runtime.docker_swarm_client.get_service_status(service_name)

    def swarm_get_failed_tasks(self, service_name: str, limit: int = 10) -> dict:
        return self.runtime.docker_swarm_client.get_failed_tasks(service_name, limit)

    def swarm_check_service_health(self, service_name: str) -> dict:
        return self.runtime.docker_swarm_client.check_service_health(service_name)

    def swarm_get_service_logs_filter(
        self,
        service_name: str,
        keyword: str,
        tail: int | None = None,
        since: str | None = None,
    ) -> dict:
        return self.runtime.docker_swarm_client.get_service_logs_filter(service_name, keyword, tail, since)

    def zabbix_get_host_storage_overview(self, host_query: str) -> dict:
        return self.runtime.zabbix_client.get_host_storage_overview(host_query)

    def zabbix_get_host_overview(self, host_query: str) -> dict:
        return self.runtime.zabbix_client.get_host_overview(host_query)

    def alerts_analyze_payload(self, raw_payload_json: str) -> dict:
        payload = json.loads(raw_payload_json)
        return self.runtime.alert_analysis_service.analyze(payload)
