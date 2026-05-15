from __future__ import annotations

import asyncio
import json
import os
import uuid


def _hide_database_url_from_chainlit() -> None:
    """Chainlit ``get_data_layer()`` 实时检查 ``os.environ.get("DATABASE_URL")``，
    一旦看到非空就 ``from .chainlit_data_layer import ChainlitDataLayer`` → 触发
    ``import asyncpg``，我们的镜像没装就 login 接口 500。

    问题：单纯在文件顶 ``os.environ.pop`` 不够——``from config import Config``
    会触发 ``load_dotenv()``，把 .env 里的 ``DATABASE_URL`` 重新写回 env，绕开了
    我们的 pop。

    解法：pop 两次（chainlit import 前一次防早期触发，所有 import 完成后再一次
    清掉 load_dotenv 重新加回去的）。备份保存到 ``PLATFORM_DATABASE_URL``，
    平台 SQL store 仍可通过 Config.DATABASE_URL 类属性使用（class 已求值完）。
    """
    val = os.environ.pop("DATABASE_URL", None)
    if val:
        os.environ.setdefault("PLATFORM_DATABASE_URL", val)


_hide_database_url_from_chainlit()

import chainlit as cl

from config import Config
from ops_agent import UnifiedOpsAgent
from ops_platform.auth import verify_password
from ops_platform.context import SkillContext
from runtime import create_runtime

# 关键二次 pop —— config 导入时 load_dotenv 把 DATABASE_URL 加回了 env
_hide_database_url_from_chainlit()


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


def _build_resource_inventory() -> dict[str, list[dict]]:
    """枚举当前平台所有已 enabled connection，按 type 分组。
    给 welcome 页 + ChatSettings 共用，避免两边各写一遍。"""
    out: dict[str, list[dict]] = {}
    try:
        conns = _runtime.connection_manager.list()
    except Exception:
        return out
    for c in conns:
        if not c.get("enabled", True):
            continue
        out.setdefault(c.get("type_code", "other"), []).append(c)
    return out


def _format_welcome_message(user: dict | None, inventory: dict[str, list[dict]]) -> str:
    """根据当前 inventory 拼出"专属给当前用户的"欢迎页 markdown。

    设计：
    - 顶部一句话身份 + 角色
    - "你能调度的集群"按 type 分组列出别名 + 节点数 / IP 等关键识别词
    - "对话技巧"给具体的、能直接复制贴的示例（基于真实集群名）
    - "写操作流程"提示
    """
    display = (user or {}).get("display_name") or "访客"
    role = (user or {}).get("role", "user")
    role_hint = "👑 管理员（可批准写操作）" if role == "admin" else "👤 普通用户"

    type_labels = {
        "host_agent":    "🖥️ 节点诊断 Agent（host_* skill）",
        "swarm":         "🐝 Docker Swarm 集群（swarm_* skill）",
        "k8s":           "☸️ K8s 集群（k8s_* skill）",
        "zabbix":        "📊 Zabbix 监控（zabbix_* skill）",
        "alert_analysis":"📥 告警分析",
        "http_api":      "🌐 HTTP API 接入",
    }
    type_order = ["host_agent", "swarm", "k8s", "zabbix", "http_api", "alert_analysis"]

    sections: list[str] = []
    sections.append(f"👋 欢迎，**{display}**（{role_hint}）")
    sections.append("")
    sections.append("我是面向运维场景的 AI 助手，**国产模型驱动 + 多集群协同诊断**。"
                    "你直接说自然语言就行，我会自动选对集群、跑相应 skill 取证。")
    sections.append("")
    sections.append("---")
    sections.append("## 🗂️ 你能调度的资源（自动路由）")
    sections.append("")
    sections.append("**说出别名 / IP / 节点名前缀，我会自动匹配到对应集群**——")
    sections.append("不需要先去设置里切。下面是当前平台已接入的全部资源：")
    sections.append("")

    has_any = False
    for t in type_order:
        if t not in inventory:
            continue
        has_any = True
        sections.append(f"### {type_labels.get(t, t)}")
        sections.append("")
        for c in inventory[t]:
            alias = c.get("alias") or c["name"]
            cfg = c.get("config") or {}
            clue = _describe_connection_for_user(t, cfg)
            default_tag = "  · 🔧 默认" if c.get("is_default") else ""
            sections.append(f"- **{alias}**{default_tag}")
            if clue:
                sections.append(f"  - {clue}")
        sections.append("")

    if not has_any:
        sections.append("（管理员还没接入任何集群，请到管理后台 → 接入管理 添加。）")
        sections.append("")

    sections.append("---")
    sections.append("## 💬 怎么说我能听懂")
    sections.append("")
    sections.append("**最快上手 5 个例子**（按你实际接入的集群拼），任选一句粘贴试：")
    sections.append("")

    # 动态生成示例：从 inventory 抓真实别名
    examples = _generate_concrete_examples(inventory)
    for ex in examples:
        sections.append(f"- {ex}")
    sections.append("")
    sections.append("---")
    sections.append("## ⚡ 写操作流程")
    sections.append("")
    sections.append("当你要求重启、扩缩容、回滚等**会改变集群状态**的操作：")
    sections.append("1. 我先给一段「拟执行 / 原因 / 影响范围 / 回滚方式」的提议")
    sections.append("2. 下方弹一张 ✅ 确认 / ❌ 拒绝 卡片")
    sections.append("3. 点确认后平台执行，我给最终总结")
    sections.append("4. **某些高危操作只有 admin 能确认**（卡片会标明）")
    sections.append("")
    sections.append("写操作全程审计落库；可在管理后台 → 调用审计 查询。")
    sections.append("")
    sections.append("---")
    sections.append("💡 **小贴士**：右上角 ⚙️ 也能手动切默认集群；不切的话，我会按"
                    "你提到的关键字自动路由，或走系统默认。")

    return "\n".join(sections)


def _describe_connection_for_user(type_code: str, cfg: dict) -> str:
    """给 welcome 页用的、人类可读的 connection 一句话描述。"""
    if type_code == "swarm":
        host = (cfg.get("docker_host") or "").replace("tcp://", "")
        return f"swarm manager：`{host}`"
    if type_code == "k8s":
        kc = cfg.get("kubeconfig") or ""
        for line in kc.splitlines():
            if "server:" in line:
                server = line.split("server:", 1)[1].strip()
                return f"K8s API：`{server}`"
        return ""
    if type_code == "host_agent":
        kind = cfg.get("kind", "?")
        transport = cfg.get("transport") or "<auto>"
        if kind == "swarm":
            host = (cfg.get("docker_host") or "").replace("tcp://", "")
            return f"swarm 节点 agent，通过 manager `{host}`，传输 `{transport}`"
        if kind == "k8s":
            kc = cfg.get("kubeconfig") or ""
            for line in kc.splitlines():
                if "server:" in line:
                    server = line.split("server:", 1)[1].strip()
                    return f"K8s DaemonSet agent，API `{server}`，传输 `{transport}`"
            return f"K8s DaemonSet agent，传输 `{transport}`"
        return f"kind=`{kind}`"
    if type_code == "zabbix":
        return f"Zabbix：`{cfg.get('base_url','')}`"
    return ""


def _generate_concrete_examples(inventory: dict[str, list[dict]]) -> list[str]:
    """根据真实 inventory 生成有用的对话示例。
    例：如果有名为 ``codewave`` 的 K8s connection，就给出
    ``"codewave 集群 default ns 的 deployment 有哪些"`` 这种具体示例。"""
    examples: list[str] = []

    # K8s 示例
    for c in inventory.get("k8s", [])[:2]:
        alias = c.get("alias") or c["name"]
        short = (c["name"] or alias).split()[0]
        examples.append(f"☸️ \"**{short}** 集群上各 namespace 有哪些 deployment\"")

    # swarm 示例
    for c in inventory.get("swarm", [])[:2]:
        alias = c.get("alias") or c["name"]
        short = (c["name"] or alias).split()[0]
        examples.append(f"🐝 \"**{short}** 上现在有多少个 service，按 stack 分组看看\"")

    # host_agent 真节点诊断
    for c in inventory.get("host_agent", [])[:2]:
        alias = c.get("alias") or c["name"]
        # 抠 short 的关键词
        short = (c["name"] or alias).split("-")[0]
        examples.append(f"🔍 \"**{short}** 集群随便一台节点磁盘怎么样\"")

    # 兜底通用
    examples.append("📥 \"这条告警怎么处理：``{...zabbix payload JSON...}``\"")
    examples.append("⚡ \"重启 xx 这个 deployment\"（会弹卡片审批，不会偷偷干）")

    return examples


@cl.on_chat_start
async def on_chat_start() -> None:
    user = _current_user()
    cl.user_session.set("platform_user", user)
    cl.user_session.set("runtime", _runtime)
    cl.user_session.set("agent", UnifiedOpsAgent(_runtime, max_steps=Config.AGENT_MAX_REASONING_STEPS))

    session_id = str(uuid.uuid4())
    cl.user_session.set("session_id", session_id)
    cl.user_session.set("selected_connections", {})

    # 持久化 session 元数据到平台 store —— 跟 chainlit 内部的内存会话区分开，
    # 平台这边的 chat_sessions / chat_messages 表是真正的持久审计层。
    try:
        _runtime.store.save_chat_session(
            session_id,
            metadata={
                "channel": "chainlit",
                "entrypoint": "chainlit_app.py",
                "user": (user or {}).get("username"),
                "role": (user or {}).get("role"),
                "started_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            },
        )
    except Exception as exc:
        # 不挂掉 chat —— 用户能继续聊天，只是会话元数据没存
        print(f"[chainlit] save_chat_session failed: {exc}")

    # 当前 inventory（重新枚举，反映 admin 后台最新接入）
    inventory = _build_resource_inventory()
    cl.user_session.set("inventory", inventory)

    # ChatSettings 下拉：支持手动切 host_agent / swarm / k8s / zabbix 默认集群
    settings_inputs = []
    for t, label in [
        ("host_agent", "🖥️ 当前节点 Agent (host_*)"),
        ("swarm",      "🐝 当前 Swarm 集群"),
        ("k8s",        "☸️ 当前 K8s 集群"),
        ("zabbix",     "📊 当前 Zabbix"),
    ]:
        opts = [
            {"label": (c.get("alias") or c["name"]), "value": c["id"], "is_default": c.get("is_default")}
            for c in inventory.get(t, [])
        ]
        if not opts:
            continue
        # 加一个 "<自动按 LLM 路由>" 选项作为默认（用户不主动切就让 LLM 自己挑）
        all_labels = ["<自动按对话内容路由>"] + [o["label"] for o in opts]
        settings_inputs.append(cl.input_widget.Select(
            id=f"{t}_connection_id",
            label=label,
            values=all_labels,
            initial_index=0,
        ))
        cl.user_session.set(f"{t}_opts", opts)

    if settings_inputs:
        await cl.ChatSettings(settings_inputs).send()

    # 动态欢迎页：列真集群 + 真示例
    welcome = _format_welcome_message(user, inventory)
    await cl.Message(content=welcome).send()


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """通用 settings 处理：支持任意 type 的手动覆盖。``<自动按对话内容路由>``
    选项不写进 selected_connections，让 LLM 走名录自动匹配。"""
    selected: dict[str, str] = {}
    for t in ("host_agent", "swarm", "k8s", "zabbix"):
        label = settings.get(f"{t}_connection_id")
        if not label or label.startswith("<自动"):
            continue
        opts = cl.user_session.get(f"{t}_opts") or []
        match = next((o for o in opts if o["label"] == label), None)
        if match:
            selected[t] = match["value"]
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
        async with cl.Step(name=step_name, type="tool", language="json") as s:
            # language='json' 让 input / output 渲染成代码块（带语法高亮 + 边框）
            # 而不是裸文本
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
    async with cl.Step(name=f"⚙️ 平台正在{action_name}写操作…", type="tool", language="json") as step:
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
