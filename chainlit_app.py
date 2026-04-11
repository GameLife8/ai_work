from __future__ import annotations

import json
import uuid

import chainlit as cl

from config import Config
from ops_agent import UnifiedOpsAgent
from runtime import create_runtime


@cl.on_chat_start
async def on_chat_start() -> None:
    runtime = create_runtime(Config)
    cl.user_session.set("agent", UnifiedOpsAgent(runtime, max_steps=Config.AGENT_MAX_REASONING_STEPS))
    session_id = str(uuid.uuid4())
    cl.user_session.set("session_id", session_id)
    cl.user_session.set("store", runtime.store)
    runtime.store.save_chat_session(
        session_id,
        metadata={
            "channel": "chainlit",
            "entrypoint": "chainlit_app.py",
        },
    )
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
    store = cl.user_session.get("store")
    session_id = cl.user_session.get("session_id")
    text = (message.content or "").strip()
    if store and session_id and text:
        store.save_chat_message(session_id, "user", text)
    try:
        outcome = agent.ask(text)
    except Exception as exc:
        if store and session_id:
            store.save_chat_message(session_id, "assistant", f"处理失败：{exc}", metadata={"error": True})
        await cl.Message(content=f"处理失败：{exc}").send()
        return

    if store and session_id:
        store.save_chat_message(
            session_id,
            "assistant",
            outcome.message,
            trace=outcome.trace,
            metadata={"trace_count": len(outcome.trace)},
        )
    await cl.Message(content=outcome.message).send()
    if outcome.trace:
        await cl.Message(
            content=f"本次共调用 {len(outcome.trace)} 个 skill，详情已收起到侧边面板。",
            elements=[
                cl.Text(
                    name="skill_trace.json",
                    content=json.dumps(outcome.trace, ensure_ascii=False, indent=2),
                    display="side",
                    language="json",
                )
            ],
        ).send()
