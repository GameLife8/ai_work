from __future__ import annotations

import chainlit as cl

from agent import SwarmAgent


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("agent", SwarmAgent())
    cl.user_session.set("pending_tool", None)
    await cl.Message(
        content=(
            "Swarm Agent 已就绪。\n\n"
            "我可以查询 Docker Swarm 服务、节点、网络、日志和 Stack 信息；"
            "涉及写操作时，我会先弹出确认。"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    agent: SwarmAgent = cl.user_session.get("agent")
    pending = cl.user_session.get("pending_tool")
    text = (message.content or "").strip()

    if pending:
        if text.lower() in {"确认", "yes", "y", "confirm"}:
            outcome = agent.execute_confirmed(
                pending["tool_name"],
                pending["tool_args"],
                pending["user_message"],
            )
            cl.user_session.set("pending_tool", None)
            await cl.Message(content=outcome.message).send()
            return
        if text.lower() in {"取消", "no", "n", "cancel"}:
            cl.user_session.set("pending_tool", None)
            await cl.Message(content="已取消本次写操作，没有对 Swarm 做任何变更。").send()
            return
        await cl.Message(content="当前有待确认的写操作，请回复“确认”或“取消”。").send()
        return

    try:
        outcome = agent.plan(text)
    except Exception as exc:
        await cl.Message(content=f"处理失败：{exc}").send()
        return

    if outcome.kind == "confirm":
        cl.user_session.set(
            "pending_tool",
            {
                "tool_name": outcome.tool_name,
                "tool_args": outcome.tool_args,
                "user_message": text,
            },
        )
        await cl.Message(content=outcome.message).send()
        return

    await cl.Message(content=outcome.message).send()
