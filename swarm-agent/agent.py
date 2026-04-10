from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from config import get_settings
from permissions import ensure_allowed, get_tool_level
from skills import TOOL_REGISTRY
from tools_schema import TOOLS


SYSTEM_PROMPT = """
你是 Docker Swarm 运维助手。

你的工作边界：
1. 你不能直接编造 Docker 状态，必须通过工具获取真实结果。
2. 你只能在给定工具中选择操作。
3. 读操作可以直接执行。
4. 写操作必须先生成确认摘要，等待用户确认。
5. 禁止操作必须直接拒绝，并解释原因。
6. 回答必须使用中文。
7. 结论中优先给出当前状态、风险、建议下一步。
""".strip()


@dataclass
class AgentOutcome:
    kind: str
    message: str
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None


class SwarmAgent:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.model_api_key or not self.settings.model_base_url:
            raise RuntimeError("模型配置缺失，请先设置 MODEL_API_KEY/MODEL_BASE_URL。")
        self.client = OpenAI(
            api_key=self.settings.model_api_key,
            base_url=self.settings.model_base_url,
        )

    def plan(self, user_message: str) -> AgentOutcome:
        completion = self.client.chat.completions.create(
            model=self.settings.model_name,
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    },
                }
                for tool in TOOLS
            ],
            tool_choice="auto",
        )
        message = completion.choices[0].message
        if not message.tool_calls:
            content = message.content or "未识别到明确操作。"
            return AgentOutcome(kind="answer", message=content)
        tool_call = message.tool_calls[0]
        tool_name = tool_call.function.name
        tool_args = json.loads(tool_call.function.arguments or "{}")
        ensure_allowed(tool_name)
        level = get_tool_level(tool_name)
        if level == "write":
            return AgentOutcome(
                kind="confirm",
                message=self._build_confirmation(tool_name, tool_args),
                tool_name=tool_name,
                tool_args=tool_args,
            )
        result = self._execute_tool(tool_name, tool_args)
        summary = self._summarize(user_message, tool_name, tool_args, result)
        return AgentOutcome(
            kind="result",
            message=summary,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=result,
        )

    def execute_confirmed(self, tool_name: str, tool_args: dict[str, Any], user_message: str) -> AgentOutcome:
        ensure_allowed(tool_name)
        result = self._execute_tool(tool_name, tool_args)
        summary = self._summarize(user_message, tool_name, tool_args, result)
        return AgentOutcome(
            kind="result",
            message=summary,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=result,
        )

    def _execute_tool(self, tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        handler = TOOL_REGISTRY[tool_name]
        return handler(**tool_args)

    def _build_confirmation(self, tool_name: str, tool_args: dict[str, Any]) -> str:
        return (
            "检测到写操作，请确认后执行。\n\n"
            f"- 操作: `{tool_name}`\n"
            f"- 参数: ```json\n{json.dumps(tool_args, ensure_ascii=False, indent=2)}\n```\n"
            "- 说明: 该操作会直接修改 Swarm 资源状态。"
        )

    def _summarize(
        self,
        user_message: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: dict[str, Any],
    ) -> str:
        completion = self.client.chat.completions.create(
            model=self.settings.model_name,
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 Docker Swarm 运维助手，请根据用户问题、工具名、工具参数和真实返回结果，"
                        "用中文生成简洁报告。报告要包含：当前状态、关键证据、风险判断、建议下一步。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": user_message,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "tool_result": tool_result,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ],
        )
        return completion.choices[0].message.content or "操作已执行，但模型未返回总结。"
