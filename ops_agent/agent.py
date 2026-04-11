from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ops_agent.model_client import OpsModelClient
from ops_agent.skills import UnifiedSkillRegistry


SYSTEM_PROMPT = """
你是统一运维智能助手。

你管理的是同一个项目下的多种能力，不要把 Zabbix、Docker Swarm、告警研判当成互相独立的系统。

你的工作边界：
1. 必须通过 skill 获取真实信息，不能编造。
2. 可以连续调用多个只读 skill 完成诊断。
3. 当容器问题涉及主机资源时，可以继续调用 Zabbix 相关 skill 补充证据。
4. 当用户想了解主机状态、磁盘、CPU、内存、可用性时，优先使用 Zabbix skill。
5. 当用户想分析服务为什么起不来、为什么异常、为什么发布失败时，优先使用 Swarm skill。
6. 当用户给你一段原始告警 payload 时，可以使用告警分析 skill。
7. 输出必须使用中文。

常见组合策略：
- 服务起不来：先 swarm_check_service_health，再 swarm_get_failed_tasks，再按 error/exception 过滤日志，必要时补 service detail。
- 如果日志或任务信息显示资源、主机、网络相关问题，再补 Zabbix 主机概览或磁盘概览。
- 主机存储问题：先 zabbix_get_host_storage_overview。
- 主机总体状态：先 zabbix_get_host_overview。

最终回答优先包含：
- 当前状态
- 检测过程
- 关键证据
- 判断结论
- 建议操作
""".strip()


@dataclass
class AgentOutcome:
    message: str
    trace: list[dict[str, Any]]


class UnifiedOpsAgent:
    def __init__(self, runtime, max_steps: int = 8) -> None:
        self.runtime = runtime
        self.max_steps = max_steps
        self.skills = UnifiedSkillRegistry(runtime)
        self.model = OpsModelClient(
            base_url=runtime.ai_client.base_url,
            api_key=runtime.ai_client.api_key,
            model=runtime.ai_client.model,
            timeout_seconds=120,
        )

    def ask(self, user_message: str) -> AgentOutcome:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        trace: list[dict[str, Any]] = []

        for _ in range(self.max_steps):
            message = self.model.create_completion(
                messages=messages,
                tools=self.skills.openai_tools(),
                tool_choice="auto",
            )
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                heuristic = self._heuristic_route(user_message)
                if heuristic and not trace:
                    tool_name, tool_args = heuristic
                    try:
                        result = self.skills.execute(tool_name, tool_args)
                    except Exception as exc:
                        result = {"tool_error": str(exc), "tool_name": tool_name, "tool_args": tool_args}
                    trace.append(
                        {
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "tool_result": result,
                        }
                    )
                    summary = self.model.create_completion(
                        messages=[
                            {
                                "role": "system",
                                "content": "请根据用户问题和 skill 结果输出中文正式报告，包含当前状态、检测过程、关键证据、判断结论、建议操作。",
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"user_message": user_message, "trace": trace},
                                    ensure_ascii=False,
                                    indent=2,
                                ),
                            },
                        ],
                    )
                    return AgentOutcome(
                        message=summary.get("content") or "已执行兜底 skill，但模型未输出总结。",
                        trace=trace,
                    )
                return AgentOutcome(
                    message=message.get("content") or "未获得明确结论。",
                    trace=trace,
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for tool_call in tool_calls:
                function_payload = tool_call.get("function", {})
                tool_name = function_payload.get("name")
                tool_args = json.loads(function_payload.get("arguments") or "{}")
                try:
                    result = self.skills.execute(tool_name, tool_args)
                except Exception as exc:
                    result = {"tool_error": str(exc), "tool_name": tool_name, "tool_args": tool_args}
                trace.append(
                    {
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "tool_result": result,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        summary = self.model.create_completion(
            messages=[
                {
                    "role": "system",
                    "content": "请根据用户问题和多次 skill 结果输出中文正式报告，包含当前状态、检测过程、关键证据、判断结论、建议操作。",
                },
                {
                    "role": "user",
                    "content": json.dumps({"user_message": user_message, "trace": trace}, ensure_ascii=False, indent=2),
                },
            ],
        )
        return AgentOutcome(
            message=summary.get("content") or "已完成排查，但模型没有输出总结。",
            trace=trace,
        )

    @staticmethod
    def _heuristic_route(user_message: str) -> tuple[str, dict[str, Any]] | None:
        service_match = re.search(r"([A-Za-z0-9][A-Za-z0-9_.-]{2,})", user_message)
        host_match = re.search(r"([A-Za-z0-9][A-Za-z0-9_.-]{2,})", user_message)

        if any(keyword in user_message for keyword in ["硬盘", "磁盘", "挂载点"]) and host_match:
            return "zabbix_get_host_storage_overview", {"host_query": host_match.group(1)}

        if any(keyword in user_message for keyword in ["主机情况", "主机状态", "主机概况", "主机概览"]) and host_match:
            return "zabbix_get_host_overview", {"host_query": host_match.group(1)}

        if any(keyword in user_message for keyword in ["起不来", "启动失败", "异常", "排查"]) and service_match:
            return "swarm_check_service_health", {"service_name": service_match.group(1)}

        payload_match = re.search(r"(\{[\s\S]*\})", user_message)
        if payload_match:
            return "alerts_analyze_payload", {"raw_payload_json": payload_match.group(1)}

        return None
