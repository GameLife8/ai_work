from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ops_platform.context import SkillContext
from ops_platform.prompts import assemble_default, assemble_from_records
from ops_platform.signals import collect as collect_signals
from ops_platform.signals import dedup_key as signal_dedup
from ops_platform.signals import render_hint as render_signal_hint


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
            {"role": "user", "content": user_message},
        ]
        trace: list[dict[str, Any]] = []
        pending_actions: list[dict[str, Any]] = []
        seen_signal_keys: set[tuple] = set()
        all_signals: list[dict[str, Any]] = []

        for _ in range(self.max_steps):
            message = self.model.create_completion(
                messages=messages, tools=tools, tool_choice="auto",
            )
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                heuristic = self._heuristic_route(user_message)
                if heuristic and not trace:
                    name, args = heuristic
                    envelope = self.invoker.invoke(name, args, ctx)
                    trace.append(self._to_trace_item(name, args, envelope))
                    if envelope.get("status") == "needs_confirmation":
                        pending_actions.append(envelope)
                    summary = self.model.create_completion(
                        messages=[
                            {"role": "system", "content":
                             "请根据用户问题和 skill 结果输出中文报告。"
                             "若 skill 结果包含 _pending=True，仅说明"
                             "你打算做什么、风险、并提示用户在下方点击确认/取消，不要再次调用工具。"},
                            {"role": "user", "content": json.dumps(
                                {"user_message": user_message, "trace": trace},
                                ensure_ascii=False, indent=2,
                            )},
                        ],
                    )
                    return AgentOutcome(
                        message=summary.get("content") or "已执行兜底 skill。",
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
