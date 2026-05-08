from __future__ import annotations

import asyncio
import json
import os
import uuid

import chainlit as cl

from config import Config
from ops_agent import UnifiedOpsAgent
from ops_platform.auth import verify_password
from ops_platform.context import SkillContext
from runtime import create_runtime


_runtime = create_runtime(Config)


# Chainlit 启用 password auth 需要 CHAINLIT_AUTH_SECRET。
# 这里复用 admin 后台的 JWT secret，避免运维人员维护两套环境变量。
os.environ.setdefault("CHAINLIT_AUTH_SECRET", Config.ADMIN_JWT_SECRET)


def _connection_options(type_code: str) -> list[dict]:
    items = _runtime.connection_manager.list(type_code=type_code)
    return [{"label": (c.get("alias") or c["name"]), "value": c["id"], "is_default": c.get("is_default")}
            for c in items if c.get("enabled", True)]


@cl.password_auth_callback
def auth_callback(username: str, password: str):
    """复用 admin 平台用户表。普通用户和 admin 都从这里登录。"""
    user = _runtime.store.get_user_by_username((username or "").strip())
    if not user or not user.get("enabled", True):
        return None
    if not verify_password(password or "", user["password_hash"]):
        return None
    return cl.User(
        identifier=user["username"],
        metadata={
            "id": user["id"],
            "role": user["role"],
            "display_name": user.get("display_name") or user["username"],
        },
    )


def _current_user() -> dict | None:
    cl_user = cl.user_session.get("user")
    if not cl_user:
        return None
    md = getattr(cl_user, "metadata", None) or {}
    return {
        "id": md.get("id"),
        "username": getattr(cl_user, "identifier", None),
        "role": md.get("role", "user"),
        "display_name": md.get("display_name"),
    }


@cl.on_chat_start
async def on_chat_start() -> None:
    user = _current_user()
    cl.user_session.set("platform_user", user)
    cl.user_session.set("runtime", _runtime)
    cl.user_session.set("agent", UnifiedOpsAgent(_runtime, max_steps=Config.AGENT_MAX_REASONING_STEPS))

    session_id = str(uuid.uuid4())
    cl.user_session.set("session_id", session_id)
    cl.user_session.set("selected_connections", {})

    _runtime.store.save_chat_session(
        session_id,
        metadata={
            "channel": "chainlit",
            "entrypoint": "chainlit_app.py",
            "user": (user or {}).get("username"),
            "role": (user or {}).get("role"),
        },
    )

    swarm_opts = _connection_options("swarm")
    zabbix_opts = _connection_options("zabbix")

    settings_inputs = []
    if swarm_opts:
        settings_inputs.append(cl.input_widget.Select(
            id="swarm_connection_id",
            label="当前 Swarm 集群",
            values=[o["label"] for o in swarm_opts],
            initial_index=next((i for i, o in enumerate(swarm_opts) if o["is_default"]), 0),
        ))
    if zabbix_opts:
        settings_inputs.append(cl.input_widget.Select(
            id="zabbix_connection_id",
            label="当前 Zabbix",
            values=[o["label"] for o in zabbix_opts],
            initial_index=next((i for i, o in enumerate(zabbix_opts) if o["is_default"]), 0),
        ))

    if settings_inputs:
        await cl.ChatSettings(settings_inputs).send()

    cl.user_session.set("swarm_opts", swarm_opts)
    cl.user_session.set("zabbix_opts", zabbix_opts)

    role = (user or {}).get("role", "user")
    display = (user or {}).get("display_name") or "访客"
    role_hint = "管理员" if role == "admin" else "普通用户"

    await cl.Message(
        content=(
            f"欢迎，{display}（{role_hint}）。\n\n"
            "右上角设置里可切换「当前 Swarm 集群 / Zabbix 实例」，所有 skill 调用会按你的选择生效。\n\n"
            "你可以直接问：\n"
            "- 某个服务为什么起不来\n"
            "- 某台主机所有硬盘当前情况\n"
            "- 某条原始告警应该怎么研判"
        )
    ).send()


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    selected: dict[str, str] = {}

    swarm_opts = cl.user_session.get("swarm_opts") or []
    zabbix_opts = cl.user_session.get("zabbix_opts") or []

    swarm_label = settings.get("swarm_connection_id")
    if swarm_label:
        match = next((o for o in swarm_opts if o["label"] == swarm_label), None)
        if match:
            selected["swarm"] = match["value"]

    zabbix_label = settings.get("zabbix_connection_id")
    if zabbix_label:
        match = next((o for o in zabbix_opts if o["label"] == zabbix_label), None)
        if match:
            selected["zabbix"] = match["value"]

    cl.user_session.set("selected_connections", selected)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    agent: UnifiedOpsAgent = cl.user_session.get("agent")
    runtime = cl.user_session.get("runtime")
    session_id = cl.user_session.get("session_id")
    selected = cl.user_session.get("selected_connections") or {}
    user = cl.user_session.get("platform_user")
    text = (message.content or "").strip()

    if runtime and session_id and text:
        runtime.store.save_chat_message(session_id, "user", text,
                                        metadata={"user": (user or {}).get("username")})

    # ---- 模型分析中：用 cl.Step 显示 spinner，agent.ask 在线程池里跑不阻塞事件循环 ----
    async with cl.Step(name="🤖 模型分析中…", type="llm") as step:
        step.input = text
        try:
            outcome = await asyncio.to_thread(
                agent.ask,
                text,
                user=user,
                session_id=session_id,
                selected_connections=selected,
            )
        except Exception as exc:
            step.output = f"处理失败：{exc}"
            if runtime and session_id:
                runtime.store.save_chat_message(session_id, "assistant", f"处理失败：{exc}",
                                                metadata={"error": True})
            await cl.Message(content=f"处理失败：{exc}").send()
            return
        step.output = (
            f"调用 {len(outcome.trace)} 个 skill；"
            f"待确认 {len(outcome.pending_actions)} 个写操作。"
        )

    # ---- 把每次 skill 调用以原生 cl.Step 形式渲染（默认折叠 + 自带耗时） ----
    for item in outcome.trace:
        sigs = item.get("signals") or []
        sig_chip = (
            "  📡 " + ", ".join(s.get("type", "") for s in sigs) if sigs else ""
        )
        step_name = (
            f"🔧 {item.get('tool_name')}"
            f"  ·  {item.get('status')}"
            f"  ·  {item.get('latency_ms', 0)}ms"
            f"{sig_chip}"
        )
        async with cl.Step(name=step_name, type="tool") as s:
            s.input = json.dumps(item.get("tool_args") or {}, ensure_ascii=False, indent=2)
            s.output = json.dumps(
                {
                    "status": item.get("status"),
                    "signals": sigs,
                    "result": item.get("tool_result"),
                },
                ensure_ascii=False, indent=2, default=str,
            )

    # ---- 最终中文报告 ----
    if runtime and session_id:
        runtime.store.save_chat_message(
            session_id, "assistant", outcome.message,
            trace=outcome.trace,
            metadata={"trace_count": len(outcome.trace)},
        )
    await cl.Message(content=outcome.message).send()

    # ---- 写操作待确认：每个 pending action 都弹一张确认卡 ----
    for pending in outcome.pending_actions:
        await _ask_confirmation(pending, agent=agent, user=user, session_id=session_id, runtime=runtime)


async def _ask_confirmation(pending: dict, *, agent: UnifiedOpsAgent, user, session_id, runtime) -> None:
    preview = pending.get("preview") or {}
    token = pending.get("pending_token")
    args_pretty = json.dumps(preview.get("args") or {}, ensure_ascii=False, indent=2)
    needs_admin = preview.get("requires_admin_approval")

    body = (
        f"⚠️ **写操作待确认**\n\n"
        f"- Skill：`{preview.get('skill_code')}`（{preview.get('skill_name')}）\n"
        f"- 接入：`{preview.get('connection_id') or '默认'}`\n"
        f"- 参数：\n```json\n{args_pretty}\n```\n"
        + ("- ⚙️ 此操作需 **管理员** 确认\n" if needs_admin else "")
        + f"- 有效期至：{pending.get('expires_at')}"
    )

    actions = [
        cl.Action(name="confirm_action", payload={"token": token}, label="✅ 确认执行"),
        cl.Action(name="reject_action", payload={"token": token}, label="❌ 拒绝"),
    ]
    res = await cl.AskActionMessage(content=body, actions=actions, timeout=300).send()
    if not res:
        await cl.Message(content="（已超时未操作，本次写操作不会执行）").send()
        return

    ctx = SkillContext(
        runtime=runtime, user=user, session_id=session_id,
        selected_connections=cl.user_session.get("selected_connections") or {},
    )
    action_name = "执行" if res.get("name") == "confirm_action" else "拒绝"
    async with cl.Step(name=f"⚙️ 平台正在{action_name}写操作…", type="tool") as step:
        if res.get("name") == "confirm_action":
            envelope = await asyncio.to_thread(runtime.skill_invoker.confirm, token, ctx)
        else:
            envelope = await asyncio.to_thread(
                runtime.skill_invoker.reject, token, ctx, "user_rejected_in_chainlit",
            )
        step.output = json.dumps(envelope, ensure_ascii=False, indent=2, default=str)

    runtime.store.save_chat_message(
        session_id, "assistant",
        json.dumps(envelope, ensure_ascii=False),
        metadata={"action_decision": res.get("name"), "token": token},
    )

    async with cl.Step(name="📝 模型整理执行结果…", type="llm"):
        follow_up = await asyncio.to_thread(
            agent.follow_up_after_action, envelope, user=user, session_id=session_id,
        )
    await cl.Message(content=follow_up).send()
