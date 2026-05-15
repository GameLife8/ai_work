from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ops_platform.context import SkillContext
from ops_platform.prompts import assemble_default, assemble_from_records
from ops_platform.signals import collect as collect_signals
from ops_platform.signals import dedup_key as signal_dedup
from ops_platform.signals import render_hint as render_signal_hint


logger = logging.getLogger(__name__)


# ----- 上下文滑窗：避免 8 步循环里 messages 越积越大把窗口撑爆 ----------- #

# JSON 序列化后总长度超过这个阈值就开始压缩。~4 char/token，50K char ≈ 12.5K token。
# 国产模型大多 32K~128K 上下文；预留余量给 system + tools schema + 当前轮输出。
MAX_MESSAGE_CHARS = 50_000

# 一次压缩里至少保留多少个"最新"消息（除去 head [system, user]）。低于这个就别压了。
KEEP_TAIL_MESSAGES = 6


def _messages_size_chars(messages: list[dict[str, Any]]) -> int:
    """估算 messages 序列化后的字符数。粗略代理 token 数。"""
    try:
        return len(json.dumps(messages, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        # 极端情况下 JSON 化失败——按上限处理强制压缩
        return MAX_MESSAGE_CHARS + 1


def _compress_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把最早的一个"轮组" (assistant w/ tool_calls + 对应 tool 回复) 折叠成一句摘要。

    OpenAI tool calling 协议约束：``assistant.tool_calls`` 必须跟对应数量的
    ``tool`` 消息成对出现，不能单独丢 tool 消息（``tool_call_id`` 会悬空）。
    所以压缩单位 = 一整个轮组。

    Args:
        messages: 当前消息列表（原位**不修改**，返回新列表）。

    Returns:
        压缩后的新列表；如果找不到可压缩的轮组（比如只剩 [system, user] +
        一个未完成轮），原样返回。
    """
    if len(messages) < 4:
        return messages
    # 跳过最前面的 [system, ...] + [user, ...] 头部
    head_end = 0
    while head_end < len(messages) and messages[head_end].get("role") in {"system", "user"}:
        head_end += 1

    # 从 head 之后找第一个"轮组" = assistant(有 tool_calls) + 紧跟的所有 tool 消息
    turn_start = head_end
    while turn_start < len(messages):
        m = messages[turn_start]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            break
        turn_start += 1
    if turn_start >= len(messages):
        return messages    # 没有可压的轮组

    turn_end = turn_start + 1
    while turn_end < len(messages) and messages[turn_end].get("role") == "tool":
        turn_end += 1
    # turn_end 现在指向下一轮 assistant（或越界）
    # 如果压完后尾部少于 KEEP_TAIL_MESSAGES，就别压了——避免压得过狠丢上下文
    if len(messages) - turn_end < KEEP_TAIL_MESSAGES:
        return messages

    # 生成摘要内容：列出本轮调了哪些 skill + 简要结果
    called: list[str] = []
    for tc in (messages[turn_start].get("tool_calls") or []):
        fn = tc.get("function") or {}
        called.append(f"{fn.get('name')}({(fn.get('arguments') or '')[:80]})")
    # tool 消息体可能很长，只取每条前 200 字符
    tool_snippets: list[str] = []
    for i in range(turn_start + 1, turn_end):
        body = messages[i].get("content") or ""
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False, default=str)
        tool_snippets.append(body[:200].replace("\n", " "))

    summary = {
        "role": "system",
        "content": (
            "[历史摘要] 之前已经调用过：" + "; ".join(called) +
            "。简要结果（每条 ≤200 char）：" + " | ".join(tool_snippets) +
            "。完整原始结果已从上下文里裁剪以节省 token，详见 trace。"
        ),
    }
    return messages[:turn_start] + [summary] + messages[turn_end:]


def _maybe_compress(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """messages 太大时迭代压缩，直到达标或没法再压。"""
    current = messages
    while _messages_size_chars(current) > MAX_MESSAGE_CHARS:
        compressed = _compress_history(current)
        if compressed is current or len(compressed) == len(current):
            break    # 没法再压
        current = compressed
    return current


_LEGACY_SYSTEM_PROMPT = """
你是统一运维智能助手，负责接管中国国产模型驱动的私有化运维平台。

## 你能动什么

平台把"接入 × Skill"做成了组合：你可以同时操作多个 Swarm 集群、多个 K8s 集群、
多个 Zabbix 实例、告警分析。所有 skill 都通过工具调用（function calling）使用，
**永远不要凭记忆回答主机/服务的真实状态，必须 skill 取证**。

## 多数运维问题不是单点的——这是核心心法

容器/Pod 异常的真实根因常常落在另一层：
- 服务起不来 → 可能是宿主机磁盘满 / OOM / 镜像拉不下来 / 配置错
- Pod CrashLoop → 可能是 limits 太低被 OOMKilled / Node DiskPressure 被驱逐
- 服务变慢 → 可能是宿主机 CPU 飙高 / 邻居噪声 / 外部依赖超时

所以你必须**跨层联动取证**：

1. 容器层信号 → 立刻判断要不要追到主机层。
   - swarm_get_failed_tasks 的 Node 列 / k8s 的 pod.node 字段 → 当作 zabbix host_query。
   - 看到 OOMKilled / exit 137 / Evicted / no space / DiskPressure / connection refused
     这类关键词时，**必须**追加一次 zabbix_get_host_overview 或 zabbix_get_host_storage_overview。
2. 主机层信号 → 反向回到容器层。
   - 主机 CPU/MEM/DISK 告警时，确认这台 Node 上跑的服务是否同步异常（swarm_list_services 过滤 Node、
     或 k8s_list_pods 在该 node 上的 pod）。
3. 告警 payload → 结构化研判后，按结果展开继续查证（用 alerts_analyze_payload）。

## 工作流

复合问题 / 不熟悉的场景：**第一步先调 ``platform_get_runbooks``** 拿对应剧本，
按里面的 step 顺序选 skill；一边取证一边把发现写进最终报告。

简单问题（直接问"列服务"、"看主机磁盘"）：直接挑对应 skill，不必先查剧本。

## 工作边界

- 必须通过 skill 获取真实信息，绝不编造数字、状态、日志。
- 可以连续调用多个只读 skill；同一个 skill 在已经拿到结果后**不要重复调用**，除非换了关键参数。
- 用户可能在会话里指定了「当前 Swarm 集群 / Zabbix 实例 / K8s 集群」；你不必为只读 skill 追问 connection_id，
  平台会按"参数 → 会话默认 → 平台默认"自动注入。但你想精确指明集群时，可以传 connection_id。
- 输出必须使用中文。

## 写操作（read_only=False）的 skill

调用后会拿到 ``status="needs_confirmation"`` 的结果。这意味着：
- 平台**不会**直接执行；正在等待用户在 UI 上点击确认/取消。
- **禁止再次调用同一个写 skill**，也禁止换个写 skill 强行兜底。
- 你应当用中文清楚说明：你打算做什么、为什么、影响范围、回滚方式，并提示「请在下方点击确认/取消」。
- 用户点确认后平台自动执行，再回到你这里给最终总结；不需要你再发起任何 tool 调用。

写操作还可能带 ``requires_admin_approval=True``——只有 admin 用户能确认。提示用户时要明确说"需 admin 审批"。

## 何时停止取证

满足任一即停：
1. 已经能给出"当前状态 / 检测过程 / 关键证据 / 判断结论 / 建议操作"五段式中文报告。
2. 同一 skill 已重试 ≥ 2 次仍无新信息。
3. 进入写操作 needs_confirmation 流程后立即停止。

## 最终报告格式

不论调了多少 skill，最终给用户的回答**必须**包含以下五段，每段 1–3 句即可：

**当前状态**：服务/Pod/主机现在是健康还是异常，关键数字（副本/重启次数/磁盘使用率…）。
**检测过程**：你按什么顺序查了哪些 skill，原因是什么。
**关键证据**：最核心的 1–3 条原始信息（错误码、日志片段、metric 数字），用 ``code 块`` 引用原文。
**判断结论**：根因是什么；如果没法 100% 确认，写"高度疑似 + 备选可能性"。
**建议操作**：具体到可执行——重启哪个服务、加多少 replica、清哪个目录。如果建议是写操作，
明确告诉用户"我可以帮你执行 ``swarm_force_update_service``，请下方点击确认"。
""".strip()


@dataclass
class AgentOutcome:
    message: str
    trace: list[dict[str, Any]]
    pending_actions: list[dict[str, Any]] = field(default_factory=list)


class UnifiedOpsAgent:
    """模型调度器；从平台 SkillRegistry 拿 tool 列表，通过 SkillInvoker 统一执行。"""

    def __init__(self, runtime, max_steps: int = 8, model_id: str | None = None) -> None:
        self.runtime = runtime
        self.max_steps = max_steps
        self.model = runtime.model_manager.get_client(model_id)
        self.registry = runtime.skill_registry
        self.invoker = runtime.skill_invoker

    def _load_system_prompt(self) -> str:
        """从 store 加载 prompt 段落，缺失/未启用的段用出厂默认兜底。

        每次 ask() 都重新组装一次——admin 改完 prompt 立即对下一次会话生效，
        不需要重启进程。这是平台从"硬编码"变"运营资产"的关键。
        """
        store = self.runtime.store
        try:
            records = store.list_prompt_segments() if hasattr(store, "list_prompt_segments") else []
        except Exception:
            records = []
        if not records:
            return assemble_default()
        return assemble_from_records(records)

    def _build_cluster_registry_prompt(self, *, selected_connections: dict[str, str]) -> str:
        """生成"当前平台可用集群名录"prompt，每次 ask 重新求值。

        目的：让 LLM 从用户口语里的别名、节点名前缀、IP 关键字 → 匹配到正确的
        ``connection_id`` 当作 skill 的 ``connection_id`` 参数传。

        渲染内容：
          - 按 type 分组列出所有 enabled 的 connection
          - 每条给：``connection_id``、别名（用户实际打出来的词）、识别线索
            （manager IP / kubeconfig server / 默认 namespace / 节点命名规律）
          - 标注 ``[默认]`` / ``[当前会话已选]`` 给 LLM 一个 fallback 优先级
          - 给一段路由规则的 hint，例：alias 命中 / IP/host 子串命中 → 用对应
            connection_id；都没命中走默认

        说明：这段也参与 prompt cache（如果模型 SDK 支持）。每次都重算所以
        admin 在管理后台改 alias 立即对下一句对话生效。
        """
        try:
            conns = self.runtime.connection_manager.list()
        except Exception:
            return "## 当前可用集群\n\n（无法列出 connection——可能 store 未就绪）"

        if not conns:
            return "## 当前可用集群\n\n（平台尚未配置任何 connection；让用户先到管理后台 → 接入管理添加）"

        # 按 type 分组
        by_type: dict[str, list[dict]] = {}
        for c in conns:
            if not c.get("enabled", True):
                continue
            by_type.setdefault(c.get("type_code", "other"), []).append(c)

        lines: list[str] = [
            "## 当前平台可用集群（动态注入）",
            "",
            "下表列出所有已接入的集群。**用户在对话里如果提到表中的别名、关键字、"
            "IP，你应当从对应行取 ``connection_id`` 作为 skill 的 ``connection_id`` "
            "参数传**。命中规则：",
            "- 用户说出别名（中文/英文）或别名里的关键字 → 走那条",
            "- 用户说出 manager IP / kubeconfig server IP / 节点 IP 前缀 → 走匹配那条",
            "- 用户提到的节点名（如 ``worker2.chinasws.com`` / ``lowcode-master01``）",
            "  能在某条的 \"节点命名规律\" 里识别 → 走那条",
            "- 都没命中 → 走该 type 的 ``[默认]``；没默认就用列表第一条",
            "",
        ]

        # 优先级：常用类型靠前
        type_order = ["host_agent", "swarm", "k8s", "zabbix", "http_api", "alert_analysis"]
        ordered_types = [t for t in type_order if t in by_type] + [
            t for t in by_type if t not in type_order
        ]

        for t in ordered_types:
            lines.append(f"### {t}")
            for c in by_type[t]:
                cid = c["id"]
                alias = c.get("alias") or c["name"]
                name = c["name"]
                cfg = c.get("config") or {}

                # 识别线索：根据 type 提取关键字段
                clue = self._connection_routing_clue(t, cfg)

                tags = []
                if c.get("is_default"):
                    tags.append("[默认]")
                if selected_connections.get(t) == cid:
                    tags.append("[当前会话已选]")
                tag_str = " ".join(tags)

                lines.append(
                    f"- **{alias}** {tag_str}  ←  ``connection_id={cid}``\n"
                    f"  - name=``{name}``，type=``{t}``"
                    + (f"\n  - 识别线索：{clue}" if clue else "")
                )
            lines.append("")

        lines.extend([
            "## 路由示例（few-shot）",
            "",
            "用户说「**bigdata6** 节点磁盘看一下」→ bigdata6 命中 `BigData Swarm` 的"
            "节点命名规律 → 调 ``host_storage_overview`` 时传该集群的 ``connection_id``。",
            "",
            "用户说「**codewave** 上 default 命名空间 pod 状态」→ codewave 命中 K8s "
            "集群的别名 → 调 ``k8s_list_pods`` 传该集群的 ``connection_id``。",
            "",
            "用户说「**192.168.2.124** 服务起不来」→ 192.168.2.x 段命中 `SWS Swarm` 的"
            "manager IP → 走 `SWS Swarm` 的 connection_id。",
            "",
            "**用户没说具体集群** → 直接走对应 type 的 ``[默认]``，不要追问用户。",
        ])

        return "\n".join(lines)

    @staticmethod
    def _connection_routing_clue(type_code: str, cfg: dict[str, Any]) -> str:
        """从 connection.config 抽出对 LLM 有用的识别线索（脱敏）。"""
        if type_code == "swarm":
            host = cfg.get("docker_host") or ""
            ip = host.replace("tcp://", "").split(":")[0]
            return f"swarm manager = ``{ip}``；节点 IP 一般跟它同段"
        if type_code == "host_agent":
            kind = cfg.get("kind", "?")
            transport = cfg.get("transport") or "<auto>"
            if kind == "swarm":
                host = cfg.get("docker_host") or ""
                ip = host.replace("tcp://", "").split(":")[0]
                return f"swarm host_agent；manager = ``{ip}``；transport={transport}"
            if kind == "k8s":
                kc = cfg.get("kubeconfig") or ""
                # 从 kubeconfig YAML 抠 server URL（粗暴 grep）
                server = ""
                for line in kc.splitlines():
                    if "server:" in line:
                        server = line.split("server:", 1)[1].strip()
                        break
                return f"k8s host_agent；API={server or '?'}；transport={transport}"
            return f"kind={kind}"
        if type_code == "k8s":
            kc = cfg.get("kubeconfig") or ""
            server = ""
            for line in kc.splitlines():
                if "server:" in line:
                    server = line.split("server:", 1)[1].strip()
                    break
            return f"K8s API server = ``{server or '?'}``"
        if type_code == "zabbix":
            return f"Zabbix = ``{cfg.get('base_url','')}``"
        if type_code == "http_api":
            return f"HTTP API base = ``{cfg.get('base_url','')}``"
        return ""

    def ask(
        self,
        user_message: str,
        *,
        user: dict[str, Any] | None = None,
        session_id: str | None = None,
        selected_connections: dict[str, str] | None = None,
    ) -> AgentOutcome:
        ctx = SkillContext(
            runtime=self.runtime,
            user=user,
            session_id=session_id,
            selected_connections=selected_connections or {},
        )
        visibility = "user" if user and user.get("role") != "admin" else None
        tools = self.registry.openai_tools(visibility=visibility)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._load_system_prompt()},
            # 关键：把"当前可用集群"动态注入第二条 system 消息——让 LLM 能从用户
            # 口语里的别名 / IP / 节点名 推断要传哪个 connection_id 给 skill。
            {"role": "system", "content": self._build_cluster_registry_prompt(
                selected_connections=selected_connections or {},
            )},
            {"role": "user", "content": user_message},
        ]
        trace: list[dict[str, Any]] = []
        pending_actions: list[dict[str, Any]] = []
        seen_signal_keys: set[tuple] = set()
        all_signals: list[dict[str, Any]] = []
        # 已经被"signal 强制 pivot"路径用过的 (skill, args_json) —— 避免同信号无限循环
        force_routed_keys: set[tuple[str, str]] = set()

        for step in range(self.max_steps):
            # ⏬ 调用 LLM 前先压缩 messages，防止 8 步循环里上下文越积越多撑爆窗口
            messages = _maybe_compress(messages)

            message = self.model.create_completion(
                messages=messages, tools=tools, tool_choice="auto",
            )
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                fallback = self._pick_fallback(
                    user_message=user_message,
                    trace=trace,
                    all_signals=all_signals,
                    force_routed_keys=force_routed_keys,
                )
                if fallback is not None:
                    name, args, reason = fallback
                    envelope = self.invoker.invoke(name, args, ctx)
                    trace.append(self._to_trace_item(name, args, envelope))
                    force_routed_keys.add(
                        (name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
                    )
                    if envelope.get("status") == "needs_confirmation":
                        pending_actions.append(envelope)
                        # 进入"待确认"路径：让模型给一段提议+风险说明就结束
                        summary = self.model.create_completion(
                            messages=messages + [{
                                "role": "system",
                                "content": (
                                    f"平台已根据 {reason} 自动调用 {name}({args})，"
                                    "结果是写操作 needs_confirmation。请用中文向用户说明："
                                    "你打算执行什么、为什么、影响范围、回滚方式。**禁止再调用任何工具**。"
                                ),
                            }],
                        )
                        return AgentOutcome(
                            message=summary.get("content") or "操作待用户确认。",
                            trace=trace,
                            pending_actions=pending_actions,
                        )
                    # 只读 fallback：把"合成的工具调用"塞进 messages，让下一轮 LLM 看到结果，
                    # 自己决定是给最终报告还是再调其它 skill。
                    synthetic_id = f"fallback_{step}"
                    messages.append({
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "id": synthetic_id, "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(args, ensure_ascii=False)},
                        }],
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": synthetic_id,
                        "content": self.invoker.serialize_for_model(envelope),
                    })
                    messages.append({
                        "role": "system",
                        "content": (
                            f"⚙️ 平台已根据 {reason} 自动调用 ``{name}`` 补一次取证。"
                            "请综合上下文给出最终五段式报告；如还有缺失再调一次 skill 即可。"
                        ),
                    })
                    # 把 fallback 命中的 signal 也吸收一次，避免下一轮再触发
                    for sig in collect_signals(envelope):
                        key = signal_dedup(sig)
                        if key not in seen_signal_keys:
                            seen_signal_keys.add(key)
                            all_signals.append(sig)
                    continue   # 让 for 循环进入下一步，给模型用新上下文重试

                # 真没 fallback：拿模型自己给的回答 / 或拿 trace 让模型再总结一次
                if trace:
                    summary = self.model.create_completion(
                        messages=[
                            {"role": "system", "content":
                             "请根据用户问题和 skill 结果输出中文五段式报告，"
                             "不要再调用任何工具。"},
                            {"role": "user", "content": json.dumps(
                                {"user_message": user_message, "trace": trace},
                                ensure_ascii=False, indent=2,
                            )},
                        ],
                    )
                    return AgentOutcome(
                        message=summary.get("content") or "已完成排查，但模型没有输出总结。",
                        trace=trace,
                        pending_actions=pending_actions,
                    )
                return AgentOutcome(
                    message=message.get("content") or "未获得明确结论。",
                    trace=trace,
                    pending_actions=pending_actions,
                )

            messages.append({"role": "assistant", "content": message.get("content") or "",
                             "tool_calls": tool_calls})

            had_pending = False
            new_signals: list[dict[str, Any]] = []
            # 同一轮里如果模型重复要求执行同 skill + 同 args（实测国产模型偶尔会），
            # 直接复用第一次的 envelope——避免双倍审计/双倍 LLM token + 双倍写操作风险。
            # key 不进 trace（trace 仍按顺序记录每个 tool_call 以便排错）。
            turn_call_cache: dict[tuple, dict[str, Any]] = {}
            for tool_call in tool_calls:
                fn = tool_call.get("function", {})
                name = fn.get("name")
                args = json.loads(fn.get("arguments") or "{}")
                dedup_key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str))
                cached = turn_call_cache.get(dedup_key)
                if cached is not None:
                    envelope = cached
                else:
                    envelope = self.invoker.invoke(name, args, ctx)
                    turn_call_cache[dedup_key] = envelope
                trace.append(self._to_trace_item(name, args, envelope))
                if envelope.get("status") == "needs_confirmation":
                    pending_actions.append(envelope)
                    had_pending = True
                # 提取本次结果里的结构化 signals，去重后留作下一轮 system 提示
                # 命中缓存的 envelope 已经在第一次执行时贡献过 signal，这里 signal_dedup
                # 会过滤掉，不会重复挂 hint。
                for sig in collect_signals(envelope):
                    key = signal_dedup(sig)
                    if key in seen_signal_keys:
                        continue
                    seen_signal_keys.add(key)
                    new_signals.append(sig)
                    all_signals.append(sig)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": self.invoker.serialize_for_model(envelope),
                })

            # ⚡ Signals 跨域硬 pivot：把"建议下一步"显式喂给模型
            if new_signals and not had_pending:
                hint = render_signal_hint(new_signals)
                if hint:
                    messages.append({"role": "system", "content": hint})

            if had_pending:
                # 写操作已挂起：让模型给用户出"提议+风险说明"，并停止 tool 循环。
                summary = self.model.create_completion(
                    messages=messages + [{
                        "role": "system",
                        "content": (
                            "上面工具结果中有 ``_pending=True`` 的写操作待确认。"
                            "请用中文向用户说明：你打算执行什么、为什么、影响范围、回滚方式。"
                            "**禁止再次调用任何工具**，让平台等待用户在 UI 上点击确认/取消。"
                        ),
                    }],
                )
                return AgentOutcome(
                    message=summary.get("content") or "操作待用户确认。",
                    trace=trace,
                    pending_actions=pending_actions,
                )

        summary = self.model.create_completion(
            messages=[
                {"role": "system", "content":
                 "请根据用户问题和多次 skill 结果输出中文正式报告。"},
                {"role": "user", "content": json.dumps(
                    {"user_message": user_message, "trace": trace},
                    ensure_ascii=False, indent=2,
                )},
            ],
        )
        return AgentOutcome(
            message=summary.get("content") or "已完成排查，但模型没有输出总结。",
            trace=trace,
            pending_actions=pending_actions,
        )

    def follow_up_after_action(
        self,
        action_envelope: dict[str, Any],
        *,
        user: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> str:
        """confirm/reject 后调用：把执行结果交给模型，输出一段中文总结给用户。"""
        summary = self.model.create_completion(
            messages=[
                {"role": "system", "content":
                 "用户刚刚确认/拒绝了一个写操作。下面是平台执行/拒绝的结果，"
                 "请用中文向用户说明结局、关键证据、后续建议。"},
                {"role": "user", "content": json.dumps(
                    {"action": action_envelope, "user": (user or {}).get("username")},
                    ensure_ascii=False, indent=2,
                )},
            ],
        )
        return summary.get("content") or "（模型未输出总结）"

    @staticmethod
    def _to_trace_item(name: str, args: dict, envelope: dict) -> dict:
        return {
            "tool_name": name,
            "tool_args": args,
            "tool_result": envelope.get("result"),
            "status": envelope.get("status"),
            "latency_ms": envelope.get("latency_ms"),
            "pending_token": envelope.get("pending_token"),
            "signals": collect_signals(envelope),
        }

    @staticmethod
    def _heuristic_route(user_message: str) -> tuple[str, dict[str, Any]] | None:
        host_match = re.search(r"([A-Za-z0-9][A-Za-z0-9_.-]{2,})", user_message)
        service_match = host_match

        if any(k in user_message for k in ["硬盘", "磁盘", "挂载点"]) and host_match:
            return "zabbix_get_host_storage_overview", {"host_query": host_match.group(1)}
        if any(k in user_message for k in ["主机情况", "主机状态", "主机概况", "主机概览"]) and host_match:
            return "zabbix_get_host_overview", {"host_query": host_match.group(1)}
        if any(k in user_message for k in ["起不来", "启动失败", "异常", "排查"]) and service_match:
            return "swarm_check_service_health", {"service_name": service_match.group(1)}

        payload_match = re.search(r"(\{[\s\S]*\})", user_message)
        if payload_match:
            return "alerts_analyze_payload", {"raw_payload_json": payload_match.group(1)}
        return None

    @classmethod
    def _pick_fallback(
        cls,
        *,
        user_message: str,
        trace: list[dict[str, Any]],
        all_signals: list[dict[str, Any]],
        force_routed_keys: set[tuple[str, str]],
    ) -> tuple[str, dict[str, Any], str] | None:
        """模型空手而归时挑一个兜底 skill。返回 ``(name, args, reason)`` 或 None。

        优先级：
          1. **会话刚开始** (trace 空) — 走关键词启发式 (``_heuristic_route``)，
             适合用户原话本身就指向某个 skill 的场景。
          2. **会话中段** (trace 非空) — 优先读未被 force-route 过的 ``critical``
             signal；若没有 critical 再降级看 warning。**只有带 ``next_skill`` 的
             signal 才能驱动 pivot**（避免递空 args 让下游 skill 崩）。

        防循环：每次 pivot 后调用方应该把 ``(skill, args_json)`` 写入
        ``force_routed_keys``，确保同一个 (skill, args) 不会被强制路由两次。
        """
        if not trace:
            heuristic = cls._heuristic_route(user_message)
            if heuristic:
                name, args = heuristic
                return name, args, "user_message 关键词启发式"
            return None

        # 中段：从最新 signal 往前找未 force-route 过的 critical 推荐
        for severity_target in ("critical", "warning"):
            for sig in reversed(all_signals):
                if (sig.get("severity") or "warning") != severity_target:
                    continue
                next_skill = sig.get("next_skill")
                next_args = sig.get("next_args") or {}
                if not next_skill:
                    continue
                key = (next_skill, json.dumps(next_args, sort_keys=True, ensure_ascii=False, default=str))
                if key in force_routed_keys:
                    continue
                reason = (
                    f"上一轮信号 ``{sig.get('type')}`` (severity={severity_target}) "
                    f"建议 next_skill={next_skill}"
                )
                return next_skill, next_args, reason
        return None
