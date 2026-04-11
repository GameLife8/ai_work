from __future__ import annotations

import json

import chainlit as cl

from config import Config
from ops_agent import UnifiedOpsAgent
from runtime import create_runtime


@cl.on_chat_start
async def on_chat_start() -> None:
    runtime = create_runtime(Config)
    cl.user_session.set("agent", UnifiedOpsAgent(runtime, max_steps=Config.AGENT_MAX_REASONING_STEPS))
    await cl.Message(
        content=(
            "统一运维助手已就绪。\n\n"
            "这里是同一个项目的统一前端入口。你可以直接问：\n"
            "- 某个服务为什么起不来\n"
            "- 某台主机所有磁盘当前情况\n"
            "- 某条原始告警应该怎么研判\n"
            "模型会自动选择 Swarm、Zabbix、告警分析等 skill 去取证。"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    agent: UnifiedOpsAgent = cl.user_session.get("agent")
    text = (message.content or "").strip()
    try:
        outcome = agent.ask(text)
    except Exception as exc:
        await cl.Message(content=f"处理失败：{exc}").send()
        return

    await cl.Message(content=outcome.message).send()
    if outcome.trace:
        await cl.Message(
            content="本次 skill 调用轨迹：\n```json\n" + json.dumps(outcome.trace, ensure_ascii=False, indent=2) + "\n```"
        ).send()
