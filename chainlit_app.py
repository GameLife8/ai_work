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

# ⚠️ 关键修正：config 导入触发的 ``load_dotenv()`` 会读 **.env 文件** 把里面的
# ``DATABASE_URL`` 填回（因为我们刚 pop 空了），这会**绕过 compose environment 的
# 覆盖**——本地测试时 compose 把 DATABASE_URL 指向本地 MySQL(db:3306)，却被 .env
# 里写死的 TiDB 地址盖掉，导致 chat 连错库（连到清空过的 TiDB → ensure_bootstrap
# 造一堆 env 种子接入，跟后台看到的完全不一致）。
#
# 第一次 pop 时保存的 ``PLATFORM_DATABASE_URL`` 才是 compose **真正注入**的值
# （environment 已经赢过 env_file）。用它强制修正 Config.DATABASE_URL。
# 生产环境没有 compose 覆盖时,两者本就一致,这步无副作用。
_compose_db_url = os.environ.get("PLATFORM_DATABASE_URL")
if _compose_db_url and _compose_db_url != Config.DATABASE_URL:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "chat: Config.DATABASE_URL 被 .env 的 load_dotenv 覆盖成 %r，"
        "强制修正回 compose 注入的 %r",
        Config.DATABASE_URL, _compose_db_url,
    )
    Config.DATABASE_URL = _compose_db_url


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


def _group_by_logical_cluster(inventory: dict[str, list[dict]]) -> list[dict]:
    """把多 type 的 connection 按"共享触发关键词"聚合成**逻辑集群**。

    规则：
    - 共享至少一个 tag → 同一个逻辑集群
    - 没 tag 的孤儿 connection → 独立成一组（unnamed group）
    - 一个 connection 可能落到多个逻辑集群（极少见，但允许）
    """
    # 先把每条 connection 按 tag 索引
    flat: list[dict] = []
    for t, items in inventory.items():
        for c in items:
            flat.append({**c, "type_code": t})

    # 用 union-find 思路：每个 tag 看成节点 connection 跟它名下所有 tag 同组
    tag_to_idx: dict[str, int] = {}
    groups: list[list[dict]] = []
    seen_no_tag: list[dict] = []

    def _merge(idx_a: int, idx_b: int) -> int:
        """把 b 的内容并到 a，返回保留的 idx。"""
        if idx_a == idx_b:
            return idx_a
        a, b = sorted((idx_a, idx_b))
        for c in groups[b]:
            if c not in groups[a]:
                groups[a].append(c)
        groups[b] = []  # 留空占位，后面过滤
        # 重指向所有指向 b 的 tag → a
        for k, v in list(tag_to_idx.items()):
            if v == b:
                tag_to_idx[k] = a
        return a

    for c in flat:
        tags = [t for t in (c.get("tags") or []) if t]
        if not tags:
            seen_no_tag.append(c)
            continue
        target_idx: int | None = None
        for tag in tags:
            if tag in tag_to_idx:
                if target_idx is None:
                    target_idx = tag_to_idx[tag]
                else:
                    target_idx = _merge(target_idx, tag_to_idx[tag])
        if target_idx is None:
            target_idx = len(groups)
            groups.append([])
        if c not in groups[target_idx]:
            groups[target_idx].append(c)
        for tag in tags:
            tag_to_idx[tag] = target_idx

    out: list[dict] = []
    for g in groups:
        if not g:
            continue
        # 收集这组的所有 tag
        all_tags: list[str] = []
        for c in g:
            for t in (c.get("tags") or []):
                if t not in all_tags:
                    all_tags.append(t)
        # 推一个"主名"——优先 host_agent 的 alias，其次任意
        primary = next((c for c in g if c["type_code"] == "host_agent"), g[0])
        out.append({
            "name": primary.get("alias") or primary["name"],
            "tags": all_tags,
            "connections": g,
        })

    # 没 tag 的孤儿单独成组（一个一组）
    for c in seen_no_tag:
        out.append({
            "name": c.get("alias") or c["name"],
            "tags": [],
            "connections": [c],
        })
    return out


def _format_welcome_message(user: dict | None, inventory: dict[str, list[dict]]) -> str:
    """根据当前 inventory 拼出"专属给当前用户的"欢迎页 markdown。

    设计：
    - 顶部一句话身份 + 角色
    - "你能调度的集群"按**逻辑集群（共享触发关键词）**聚合 —— 一个集群可能
      同时有 host_agent + swarm + k8s 类 connection，用户看上去是一个集群
    - 对话示例基于真实关键词
    - 写操作流程
    """
    display = (user or {}).get("display_name") or "访客"
    role = (user or {}).get("role", "user")
    role_hint = "👑 管理员（可批准写操作）" if role == "admin" else "👤 普通用户"

    type_icon = {
        "host_agent":    "🖥️",
        "swarm":         "🐝",
        "k8s":           "☸️",
        "zabbix":        "📊",
        "alert_analysis":"📥",
        "jenkins":       "🔧",
    }
    type_purpose = {
        "host_agent":    "host_* skill（进宿主取证）",
        "swarm":         "swarm_* skill（服务/任务）",
        "k8s":           "k8s_* skill（pod/deployment）",
        "zabbix":        "zabbix_* skill（监控数据）",
        "alert_analysis":"告警预分析",
        "jenkins":       "jenkins_query（CI/CD）",
    }

    sections: list[str] = []
    sections.append(f"👋 欢迎，**{display}**（{role_hint}）")
    sections.append("")
    sections.append("我是面向运维场景的 AI 助手，**国产模型驱动 + 多集群协同诊断**。"
                    "在对话里说出**触发关键词**（下面每个集群有标），我会自动选对集群、跑对应 skill。")
    sections.append("")
    sections.append("---")
    sections.append("## 🗂️ 你能调度的逻辑集群（按关键词分组）")
    sections.append("")

    clusters = _group_by_logical_cluster(inventory)
    has_any = False

    for cluster in clusters:
        has_any = True
        # 跳过 alert_analysis 这种"内置"非物理集群
        if all(c["type_code"] == "alert_analysis" for c in cluster["connections"]):
            continue
        tags = cluster["tags"]
        sections.append(f"### {cluster['name']}")
        if tags:
            kw_inline = " ".join(f"`{t}`" for t in tags)
            sections.append(f"🏷️ 关键词：{kw_inline}  ← **在对话里说出任一关键词即可路由到本集群**")
        else:
            sections.append("⚠️ 未配置触发关键词（建议到管理后台 → 接入管理 → 编辑设置）")
        sections.append("")
        # 该集群下每条 connection 的 type / 用途
        for c in cluster["connections"]:
            t = c["type_code"]
            icon = type_icon.get(t, "·")
            purpose = type_purpose.get(t, "")
            cfg = c.get("config") or {}
            clue = _describe_connection_for_user(t, cfg)
            default_mark = "  · 🔧 该 type 默认" if c.get("is_default") else ""
            sections.append(f"- {icon} **{c.get('alias') or c['name']}** — {purpose}{default_mark}")
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
    """根据真实逻辑集群（按 tags 聚合）生成有用的对话示例。
    优先用集群的关键词当锚——这样示例里的关键词跟欢迎页的"路由词"一一对应。"""
    examples: list[str] = []

    clusters = _group_by_logical_cluster(inventory)
    # 排除 alert_analysis 那种内置组
    real_clusters = [
        cl for cl in clusters
        if not all(c["type_code"] == "alert_analysis" for c in cl["connections"])
    ]

    def _kw(cluster: dict) -> str:
        # 选第一个 tag 当示例关键词；没 tag 就用名字头一个词
        if cluster["tags"]:
            return cluster["tags"][0]
        return (cluster["name"] or "").split()[0]

    # 用关键词组示例 —— 每个集群挑一个最贴合的 skill 场景
    for cluster in real_clusters[:4]:
        kw = _kw(cluster)
        if not kw:
            continue
        types = {c["type_code"] for c in cluster["connections"]}
        if "host_agent" in types:
            examples.append(f"🔍 \"**{kw}** 集群随便一台节点根分区使用率如何\"")
        elif "k8s" in types:
            examples.append(f"☸️ \"**{kw}** 上 default 命名空间的 deployment 列一下\"")
        elif "swarm" in types:
            examples.append(f"🐝 \"**{kw}** 上现在有多少 service，按 stack 分组看看\"")

    # 兜底通用
    examples.append("📥 \"这条告警怎么处理：``{...zabbix payload JSON...}``\"")
    examples.append("⚡ \"重启 xx 这个 deployment\"（会弹卡片审批，不会偷偷干）")

    return examples


def _short_session_title(session: dict, runtime) -> str:
    """从会话取一个可读标题:首条 user 消息前 30 字。"""
    sid = session["session_id"]
    try:
        msgs = runtime.store.list_chat_messages(sid, limit=1, exclude_summary=True)
        for m in msgs:
            if m.get("role") == "user" and (m.get("content") or "").strip():
                first = m["content"].strip().replace("\n", " ")
                return (first[:30] + "…") if len(first) > 30 else first
    except Exception:
        pass
    return "(空会话)"


def _format_session_label(s: dict, runtime) -> str:
    """单行可读 label,用在 ChatSettings 下拉里。

    格式:``2026-05-21 03:01 · bigdata-swarm 巡检… · 1cfe6f48``
    """
    title = _short_session_title(s, runtime)
    last = (s.get("last_message_at") or s.get("created_at") or "")[:16].replace("T", " ")
    sid = s["session_id"]
    return f"{last} · {title} · {sid[:8]}"


async def _start_new_session(user: dict) -> str:
    """生成新 uuid + 设为当前会话,返回 session_id。

    ⚠️ **不在这里写 chat_session 行**。否则每次打开/刷新页面(on_chat_start 默认
    都会开新会话)都会凭空落一条空会话,把历史下拉塞满"(空会话)"。改成**懒持久化**:
    真正第一条消息进来时(on_message)才落 session 行,并带上 owner metadata。
    """
    sid = str(uuid.uuid4())
    cl.user_session.set("session_id", sid)
    return sid


async def _resume_session(sid: str, user: dict) -> None:
    """绑定到已有 session_id + 在 UI 上重放最近 N 条消息让用户看到上下文。"""
    cl.user_session.set("session_id", sid)
    # 注意:**不**在这里设 session_persisted/owner_saved 跳过 owner 写入——
    # resume 的会话若当初是用空 metadata 建的,得靠下一条消息的 owner 写入补上 user,
    # 否则它永远在 list_chat_sessions_by_user 里查不到(老 bug)。
    try:
        msgs = _runtime.store.list_chat_messages(sid, limit=40, exclude_summary=True)
    except Exception as exc:
        await cl.Message(content=f"⚠️ 无法读取会话历史:{exc}").send()
        return
    await cl.Message(
        content=f"♻️ 已恢复会话 `{sid[:8]}…`,下面是最近 {len(msgs)} 条上下文:",
    ).send()
    for m in msgs:
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if not content or role not in ("user", "assistant"):
            continue
        # chainlit 的 cl.Message 默认就是 assistant 视觉;user 消息没法完美还原,
        # 用 author + 前缀让用户看出是谁说的。
        prefix = "👤 你:" if role == "user" else ""
        await cl.Message(
            content=(prefix + "\n\n" + content) if prefix else content,
            author=("user" if role == "user" else "assistant"),
        ).send()
    await cl.Message(content="── 历史结束,你可以继续提问 ──").send()


_RESUME_NEW = "➕ 开始新会话"
_RESUME_KEEP = "<继续当前会话>"
_DELETE_NONE = "<不删除>"


async def _setup_chat_settings_for_clusters(user: dict) -> None:
    """ChatSettings 顶栏齿轮:
       1. 切 host_agent / swarm / k8s / zabbix 默认集群
       2. **会话历史**——切到任意历史会话 / 删除会话

    设计选择:用顶栏齿轮里的 Select 而不是在消息流堆几十个按钮——
    后者实测视觉混乱(用户反馈过)。chainlit 没原生侧边栏组件,这是
    最接近"可伸缩控制面板"的 native 做法。
    """
    inventory = _build_resource_inventory()
    cl.user_session.set("inventory", inventory)

    settings_inputs: list = []
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
        all_labels = ["<自动按对话内容路由>"] + [o["label"] for o in opts]
        settings_inputs.append(cl.input_widget.Select(
            id=f"{t}_connection_id",
            label=label,
            values=all_labels,
            initial_index=0,
        ))
        cl.user_session.set(f"{t}_opts", opts)

    # ---- 会话历史下拉 ---- #
    username = (user or {}).get("username") or ""
    sessions: list[dict] = []
    if username:
        try:
            sessions = _runtime.store.list_chat_sessions_by_user(username, limit=20)
        except Exception as exc:    # noqa: BLE001
            print(f"[chainlit] list_chat_sessions_by_user failed: {exc}")

    # 即使没历史也加这两个 select,让 UI 永远一致 + 给"新会话"按钮的位置
    history_labels = [_format_session_label(s, _runtime) for s in sessions]
    label_to_sid = {lbl: s["session_id"] for lbl, s in zip(history_labels, sessions)}
    cl.user_session.set("history_label_to_sid", label_to_sid)

    resume_values = [_RESUME_KEEP, _RESUME_NEW] + history_labels
    settings_inputs.append(cl.input_widget.Select(
        id="resume_session",
        label=f"📜 会话历史 (共 {len(sessions)} 条) — 切换或新建",
        values=resume_values,
        initial_index=0,
    ))
    cl.user_session.set("last_resume_choice", _RESUME_KEEP)

    if history_labels:
        delete_values = [_DELETE_NONE] + history_labels
        settings_inputs.append(cl.input_widget.Select(
            id="delete_session",
            label="🗑️ 删除会话(立即生效,不可恢复)",
            values=delete_values,
            initial_index=0,
        ))
        cl.user_session.set("last_delete_choice", _DELETE_NONE)

    if settings_inputs:
        await cl.ChatSettings(settings_inputs).send()


@cl.on_chat_start
async def on_chat_start() -> None:
    user = _current_user()
    cl.user_session.set("platform_user", user)
    cl.user_session.set("runtime", _runtime)
    cl.user_session.set("agent", UnifiedOpsAgent(_runtime, max_steps=Config.AGENT_MAX_REASONING_STEPS))
    cl.user_session.set("selected_connections", {})

    # 装 ChatSettings(切集群 + 会话历史下拉)
    # —— 不再用消息流堆按钮(实测丑且按钮太多),改成顶栏齿轮里的紧凑控制面板
    await _setup_chat_settings_for_clusters(user)

    # 默认开新会话,不打扰 — 想管理历史请点右上角齿轮 ⚙️
    await _start_new_session(user)

    # 短欢迎语 + 提示历史会话在哪
    username = (user or {}).get("username") or ""
    history_count = 0
    if username:
        try:
            history_count = len(_runtime.store.list_chat_sessions_by_user(username, limit=20))
        except Exception:
            history_count = 0

    inventory = cl.user_session.get("inventory") or {}
    welcome = _format_welcome_message(user, inventory)
    if history_count > 0:
        # ⚠️ chainlit 2.x 的 ChatSettings 入口在**底部输入框左侧**的 ⚙️ 图标
        # (跟 📎 附件图标在一行),不是浏览器右上角。指引措辞必须精确,
        # 否则用户找不到。
        welcome = (
            f"> 💡 你有 **{history_count}** 个历史会话。\n"
            f"> 在**底部输入框左下**找到 ⚙️ 图标(跟 📎 附件并排)→ "
            f"打开后会看到「📜 会话历史」「🗑️ 删除会话」两个下拉,选了立刻生效。\n"
            f"\n"
            + welcome
        )
    await cl.Message(content=welcome).send()


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """通用 settings 处理:
       1. 集群手动覆盖(host_agent / swarm / k8s / zabbix)
       2. 会话历史:切换 / 删除

    会话切换 / 删除靠"值跟上次比对",变了就触发。这是 ChatSettings 没有按钮
    类型组件的折中——Select 改了 → on_settings_update 触发 → 检测差异。
    """
    # ---- 1. 集群覆盖 ---- #
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

    user = cl.user_session.get("platform_user") or {}
    label_to_sid = cl.user_session.get("history_label_to_sid") or {}

    # ---- 2. 会话切换 / 新建 ---- #
    resume_choice = settings.get("resume_session")
    last_resume = cl.user_session.get("last_resume_choice") or _RESUME_KEEP
    if resume_choice and resume_choice != last_resume:
        cl.user_session.set("last_resume_choice", resume_choice)
        if resume_choice == _RESUME_NEW:
            await _start_new_session(user)
            await cl.Message(content="✨ 已开始新会话,可以接着提问。").send()
        elif resume_choice in label_to_sid:
            sid = label_to_sid[resume_choice]
            await _resume_session(sid, user)
        # _RESUME_KEEP 不动

    # ---- 3. 会话删除 ---- #
    delete_choice = settings.get("delete_session")
    last_delete = cl.user_session.get("last_delete_choice") or _DELETE_NONE
    if delete_choice and delete_choice != last_delete and delete_choice != _DELETE_NONE:
        cl.user_session.set("last_delete_choice", delete_choice)
        sid = label_to_sid.get(delete_choice)
        if sid:
            try:
                removed = _runtime.store.delete_chat_session(sid)
            except Exception as exc:    # noqa: BLE001
                await cl.Message(content=f"⚠️ 删除失败:{exc}").send()
                return
            # 如果删的是当前会话,自动开新会话避免后续消息写到已删的 sid
            current_sid = cl.user_session.get("session_id")
            if current_sid == sid:
                await _start_new_session(user)
                await cl.Message(
                    content=f"🗑️ 已删除当前会话 `{sid[:8]}`(连同 {removed} 条消息),已自动为你开新会话。",
                ).send()
            else:
                await cl.Message(
                    content=f"🗑️ 已删除会话 `{sid[:8]}`(连同 {removed} 条消息)。"
                            f"刷新页面可让齿轮里的下拉同步移除该条。",
                ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    agent: UnifiedOpsAgent = cl.user_session.get("agent")
    runtime = cl.user_session.get("runtime")
    session_id = cl.user_session.get("session_id")
    selected = cl.user_session.get("selected_connections") or {}
    user = cl.user_session.get("platform_user")
    text = (message.content or "").strip()

    if runtime and session_id and text:
        # 确保会话带上 owner metadata。list_chat_sessions_by_user 靠 metadata.user 过滤,
        # 缺了用户历史就查不到该会话("凭空消失")。
        #
        # ⚠️ **不能**用 session_persisted 门控(老 bug 根因):_resume_session 会把它置 True,
        # 导致 resume 后首条消息跳过 owner 写入,紧接着 save_chat_message 内部的 keep-alive
        # ``save_chat_session(sid)``(metadata=None)若该行不存在就 INSERT 一个空 ``{}`` →
        # metadata.user 为空 → 会话在历史里查不到(昨天会话没存住的真因)。
        #
        # 改成**按 session_id 维度只写一次** owner metadata,且**先于** save_chat_message。
        # save_chat_session(metadata=) 幂等:既能建新行,也能给缺 owner 的旧行补上。
        username = (user or {}).get("username")
        owner_key = f"owner_saved:{session_id}"
        if username and not cl.user_session.get(owner_key):
            try:
                runtime.store.save_chat_session(session_id, metadata={
                    "channel": "chainlit",
                    "entrypoint": "chainlit_app.py",
                    "user": username,
                    "role": (user or {}).get("role"),
                    "started_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                })
                cl.user_session.set(owner_key, True)
            except Exception as exc:    # noqa: BLE001
                print(f"[chainlit] ensure session owner failed: {exc}")
        runtime.store.save_chat_message(session_id, "user", text,
                                        metadata={"user": username})

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
        meta: dict = {"trace_count": len(outcome.trace)}
        # 把本次 ask() 的 LLM token usage 落到 metadata 一并存——后续可走
        # admin UI 聚合（按 session / user / day）做成本分账 + 异常预警。
        if outcome.usage:
            meta["usage"] = outcome.usage
        runtime.store.save_chat_message(
            session_id, "assistant", outcome.message,
            trace=outcome.trace,
            metadata=meta,
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

    # 顶部加一行**强调按钮**的指引——避免「模型已经写了一大段说明,卡片按钮被挤到屏幕外
    # 用户没看到就超时」这种历史 UX 坑。
    body = (
        f"## 👇 点击下方 ✅ 或 ❌ 按钮做出选择\n\n"
        f"⚠️ **写操作待确认**\n\n"
        f"- Skill：`{preview.get('skill_code')}`（{preview.get('skill_name')}）\n"
        f"- 接入：`{preview.get('connection_id') or '默认'}`\n"
        f"- 参数：\n```json\n{args_pretty}\n```\n"
        + ("- ⚙️ 此操作需 **管理员** 确认\n" if needs_admin else "")
        + f"- 有效期至：{pending.get('expires_at')}\n\n"
        + f"_未在 30 分钟内选择将默认放弃_"
    )

    actions = [
        cl.Action(name="confirm_action", payload={"token": token}, label="✅ 确认执行"),
        cl.Action(name="reject_action", payload={"token": token}, label="❌ 拒绝"),
    ]
    # timeout 1800s = 30 分钟。300s(5 分钟)实测太短——模型生成的说明往往拖一段,
    # 等到用户看完往下滚到按钮可能已超过 5 分钟。
    res = await cl.AskActionMessage(content=body, actions=actions, timeout=1800).send()
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

    async with cl.Step(name="📝 模型整理执行结果…", type="llm"):
        follow_up = await asyncio.to_thread(
            agent.follow_up_after_action, envelope, user=user, session_id=session_id,
        )

    # 落库:assistant 正文 = 模型的执行总结(resume 时能重放——修"确认/拒绝后内容被截断"的根因:
    # 这段 follow_up 以前只发 UI、从不入库,一 resume 就没了)。原始 envelope 挂在 ``trace``
    # 里(admin 可回放),**不再当正文存**——否则 resume 重放历史会冒出一坨生 JSON。
    if runtime and session_id:
        try:
            runtime.store.save_chat_message(
                session_id, "assistant", follow_up,
                trace=[{
                    "tool_name": f"confirm:{res.get('name')}",
                    "tool_result": envelope,
                    "status": envelope.get("status"),
                }],
                metadata={"action_decision": res.get("name"), "token": token},
            )
        except Exception as exc:    # noqa: BLE001
            print(f"[chainlit] save follow-up failed: {exc}")
    await cl.Message(content=follow_up).send()
