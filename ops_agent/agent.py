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


# ----- 会话记忆：跨轮上下文（同会话多轮对话保持记忆）---------------------- #

# 每次 ask() 进来从 DB 拉最近 N 条原文塞进 prompt（user/assistant 文本，不含 tool 调用）
CHAT_HISTORY_RECENT = 20

# 当未摘要的历史消息数超过此阈值，自动调用模型生成新摘要，把"摘要边界之前"的对话
# 压缩成一段 200 字以内的中文摘要，落库 role=summary。
# 设计：30 条 ≈ 15 轮，足够覆盖大部分故障诊断的"主线 + 关键证据 + 结论"
CHAT_HISTORY_SUMMARIZE_AT = 30

# 摘要 prompt 模板——给模型一段对话原文，让它压成结构化摘要
_SUMMARY_PROMPT = (
    "你是 AI 运维助手的会话摘要生成器。下面是用户与助手的多轮对话原文。"
    "请压缩成不超过 250 字的中文摘要，保留以下要点：\n"
    "  - 用户问题主线（一句话）\n"
    "  - 已诊断到的关键事实（如节点名 / 服务名 / 异常类型 / 已定位的根因）\n"
    "  - 用户表达的偏好或操作约束（'不要重启' / '只在维护窗口' 等）\n"
    "  - 已经下结论的部分，避免下次重复诊断\n"
    "**省略**：寒暄、具体命令输出、调试细节、未确认的猜测。\n"
    "直接给摘要文本，不要其它前言/后语。\n\n对话原文：\n"
)


# ----- 意图分类 + 输出模式动态注入 ---------------------------------------- #
#
# 设计：基于用户当前消息的中文关键词识别 8 类意图，命中后返回一段"重申输出模式"
# 的 system 提示。这段紧贴当前 user message 注入，比开头长篇 system prompt 更显眼，
# 显著提升模型遵循对应输出模式的概率。
#
# 关键词按"专属程度"匹配——查看类（"看一下"+"配置/YAML/原文"组合）专属性强，
# 优先匹配；闲聊类专属性弱，最后兜底。

# 触发词字典：意图 → 关键词列表（OR 关系）
# 注意：``config_view`` 用 verb + noun **双命中**机制（见 _classify_intent），
# 不能放在这里走单关键词逻辑——用户经常说"配置可以展示一下"（noun 在前 verb 在后），
# 单关键词"展示配置"按字面顺序匹配会漏。
_INTENT_KEYWORDS: dict[str, list[str]] = {
    # F 类：异步长任务
    "async_task": [
        "异步", "长命令", "跑一会", "后台跑",
        "du -sh", "find /", "journalctl", "tcpdump",
        "看刚才那个任务", "任务跑完了吗", "任务结果",
    ],
    # E 类：写操作
    "write_action": [
        "重启服务", "扩容", "缩容", "扩到", "缩到", "扩成", "改成",
        "回滚", "删除服务", "下线服务", "强制更新", "改镜像",
        "替换镜像", "重新部署", "升级版本",
    ],
    # D 类：资源 / 监控
    "monitor": [
        "CPU", "cpu", "内存", "memory", "磁盘", "disk", "IO", "io",
        "负载", "load", "趋势", "峰值", "近 1 小时", "近 N 小时", "网络流量",
    ],
    # C 类：列表 / 状态总览
    "list_state": [
        "列一下", "列出", "都有哪些", "有哪些", "多少个",
        "全部服务", "全部 pod", "全部 deployment", "所有",
        "状态总览", "概况", "概览",
    ],
    # G 类：知识 / 引导
    "knowledge": [
        "怎么排查", "怎么诊断", "怎么用", "应该用什么",
        "有哪些剧本", "有哪些 runbook", "有哪些工具",
        "教我", "推荐一个", "建议怎么",
    ],
    # H 类：闲聊 / 不明
    "chat_intro": [
        "你好", "你是谁", "你能做什么", "你能干啥", "能问什么",
        "介绍下", "你有什么功能",
    ],
    # A 类（诊断）兜底，靠关键词识别强信号
    "diagnose": [
        "为什么", "起不来", "启动失败", "失败", "异常", "报错", "出错",
        "crashloop", "CrashLoop", "OOM", "oomkilled", "evicted",
        "连不上", "不通", "丢包", "502", "504", "卡顿", "慢",
        "排查", "诊断", "分析下", "看看怎么回事",
    ],
}


# B 类（查看/配置）专用：verb + noun 双命中（任意 verb 任意 noun 同时出现就命中）
#
# 因为中文里用户经常分开说："配置可以展示一下" / "展示一下配置" / "把配置给我看看" /
# "k8s 的 coredns ConfigMap 看下"——单关键词"展示配置"按字面顺序匹配只能覆盖一种。
# 拆成动词组 + 名词组，**只要句中同时有一个动词和一个名词就命中**，覆盖率高得多。
_CONFIG_VIEW_VERBS = (
    "展示", "看一下", "看下", "看看", "瞧一下", "给我看", "拿出来",
    "贴一下", "贴出来", "出示", "显示", "查看", "查一下", "show", "查询配置",
    # 单字"看" 与单字"贴"太宽，不放——靠"看一下/看下/看看"已覆盖
)
_CONFIG_VIEW_NOUNS = (
    "配置", "config", "yaml", "json", "原文", "原始", "raw", "spec",
    "configmap", "Secret", "secret",
    "环境变量", "挂载", "镜像", "Corefile", "manifest",
    "stack", "compose", "Deployment yaml", "deploy yaml",
)


_INTENT_HINTS: dict[str, str] = {
    "config_view": (
        "🎯 **本次用户意图：查看 / 配置 / 看原文（B 类）**\n"
        "**必须按这个格式输出**：\n"
        "1. 先用 ```yaml 或 ```json 代码块原样贴 skill 返回的原始内容（取 result.parsed "
        "或 result.stdout）。完整、不重排、不省字段。如果太长就贴关键段。\n"
        "2. 再用 1–3 段中文逐项翻译关键配置（image / replicas / networks / labels / "
        "placement / healthcheck / env / mounts）。\n"
        "3. 可选给一句运维建议。\n"
        "**严禁**跳过原文直接概括成「该服务是 X，运行健康……」。"
    ),
    "list_state": (
        "🎯 **本次用户意图：列表 / 状态总览（C 类）**\n"
        "**必须按这个格式输出**：\n"
        "1. 用 markdown 表格列关键字段（名字 / 状态 / 副本 / 重启次数 / 创建时间 / Node）。\n"
        "2. 异常项行首加 ⚠️ 或 🚨；行末注一句原因。\n"
        "3. 表格下方给 1–2 句总览结论：「共 N 个，M 个异常」。\n"
        "4. 有异常时追问：「想详细看哪一个？」\n"
        "**严禁**写成段落式分析报告。"
    ),
    "monitor": (
        "🎯 **本次用户意图：资源 / 性能监控（D 类）**\n"
        "**必须按这个格式输出**：\n"
        "1. markdown 表格展示「指标 / 当前值 / 阈值 / 状态(✅/⚠️/🚨)」。\n"
        "2. 1–2 段解读：哪个指标接近/超阈，趋势怎样，可能影响什么。\n"
        "3. 一句下一步建议。\n"
        "**严禁**只贴一大段文字描述。"
    ),
    "write_action": (
        "🎯 **本次用户意图：执行写操作（E 类）**\n"
        "调用对应写 skill 后会返回 ``status=needs_confirmation``。**禁止再调任何 tool**。\n"
        "用中文清楚说明四点：(1) 打算做什么 (2) 为什么 (3) 影响范围 (4) 回滚方式，\n"
        "然后提示「请在下方点击确认/取消」。如果 ``requires_admin_approval=True``，\n"
        "明确说「需 admin 审批」。"
    ),
    "async_task": (
        "🎯 **本次用户意图：异步长任务（F 类）**\n"
        "估计 > 30s 的命令一律用 ``host_run_command_async``。\n"
        "提交时给「task_id + 节点 + 命令 + 预计时长」的简短回执，结束本轮。\n"
        "查询任务时调 ``host_check_task``，把 stdout 用代码块贴出来。"
    ),
    "knowledge": (
        "🎯 **本次用户意图：知识 / 引导（G 类）**\n"
        "用结构化清单输出：(1) 一句话引言；(2) 编号的步骤列表（每步对应 skill）；\n"
        "(3) 关键注意点；(4) 如果有现成 runbook，告诉用户直接调 ``platform_run_runbook``。"
    ),
    "chat_intro": (
        "🎯 **本次用户意图：闲聊 / 自介（H 类）**\n"
        "简短一句自我介绍，列 4–6 个场景化能力（诊断 / 看配置 / 查状态 / 监控 / "
        "执行操作 / 长任务），邀请用户给具体场景。\n"
        "**严禁**堆砌 skill 列表 / 长篇大论。"
    ),
    "diagnose": (
        "🎯 **本次用户意图：诊断 / 排障（A 类）**\n"
        "按五段式中文报告输出：**当前状态** / **检测过程** / **关键证据**（用代码块引用原文）"
        " / **判断结论**（不确定就写「高度疑似 + 备选」）/ **建议操作**（具体可执行）。"
    ),
}


def _classify_intent(user_message: str) -> str | None:
    """识别用户当前消息的意图标签（8 类之一）。

    匹配优先级：config_view（强专属，verb+noun 双命中）> async_task > write_action >
    monitor > list_state > knowledge > chat_intro > diagnose。
    """
    if not user_message:
        return None
    text = user_message.lower()

    # config_view 用 verb+noun 双命中（最高优先级）
    has_verb = any(v.lower() in text for v in _CONFIG_VIEW_VERBS)
    has_noun = any(n.lower() in text for n in _CONFIG_VIEW_NOUNS)
    if has_verb and has_noun:
        return "config_view"

    # 其余意图用单关键词 OR 匹配
    priority = ["async_task", "write_action", "monitor",
                "list_state", "knowledge", "chat_intro", "diagnose"]
    for intent in priority:
        for kw in _INTENT_KEYWORDS.get(intent, []):
            if kw.lower() in text:
                return intent
    return None


def _classify_intent_hint(user_message: str) -> str | None:
    """根据用户消息返回对应输出模式的强化提示（注入到 system role）。"""
    intent = _classify_intent(user_message)
    return _INTENT_HINTS.get(intent) if intent else None


def _is_config_query_trace_item(item: dict) -> bool:
    """trace 里这一条是不是「查看配置类」的调用？

    用作 _augment_message_for_intent 的 trace 兜底——即使意图分类没识别到
    config_view（关键词漏匹配），只要 trace 里有"明显的配置查询调用"，平台
    也应该兜底贴原文。

    判定标准：
    - kube_query verb=get/describe，resource 是 configmap/secret/deploy/...
    - swarm_query verb=inspect（service/stack/network/...）
    - host_query 调 cat 类命令读 /etc/... 配置文件
    """
    if item.get("status") != "ok":
        return False
    tool = item.get("tool_name", "")
    args = item.get("tool_args") or {}
    if tool == "kube_query":
        verb = (args.get("verb") or "").lower()
        resource = (args.get("resource") or "").lower()
        if verb in ("get", "describe") and resource in (
            "configmap", "configmaps", "cm",
            "secret", "secrets",
            "deploy", "deployment", "deployments",
            "service", "services", "svc",
            "ingress", "ingresses", "ing",
            "statefulset", "statefulsets", "sts",
            "daemonset", "daemonsets", "ds",
            "pv", "pvc", "pod", "pods",
        ):
            return True
    if tool == "swarm_query":
        verb = (args.get("verb") or "").lower()
        if verb == "inspect":
            return True
    if tool == "host_query":
        cmd = (args.get("command") or "").strip()
        # cat / head / tail 读配置文件
        if cmd.startswith(("cat ", "head ", "tail ")) and ("/etc/" in cmd or "/conf" in cmd):
            return True
    return False


def _augment_message_for_intent(message: str, trace: list[dict], user_message: str) -> str:
    """**平台保证机制**：模型偷懒不照输出模式办的时候，平台兜底补全。

    触发条件（任一即可）：
    1. 意图分类识别为 config_view
    2. trace 里有"明显的配置查询"调用（_is_config_query_trace_item）

    满足触发但回答里没有 ``` 代码块 → 自动从 trace 抽 stdout/parsed 拼末尾。

    第 2 条是兜底中的兜底——关键词识别永远有漏（用户表达千变万化），
    但 trace 里的真实调用是确定性证据。
    """
    if not message:
        return message
    intent = _classify_intent(user_message)
    # 触发条件 1：意图命中 config_view
    triggered = (intent == "config_view")
    # 触发条件 2：trace 里有明显的配置查询调用
    config_query_item = None
    for item in reversed(trace or []):
        if _is_config_query_trace_item(item):
            config_query_item = item
            triggered = True
            break
    if not triggered:
        return message
    # 已有代码块 → 模型自己贴了，不动
    if "```" in message:
        return message

    # 从 trace 里抽 raw 内容——优先用 _is_config_query_trace_item 命中的那条
    raw_text = ""
    raw_lang = "yaml"
    candidates = ([config_query_item] if config_query_item else []) + list(reversed(trace or []))
    for item in candidates:
        if not item or item.get("status") != "ok":
            continue
        tool = item.get("tool_name", "")
        if tool not in ("kube_query", "swarm_query", "host_query"):
            continue
        result = item.get("tool_result") or {}
        stdout = result.get("stdout") or ""
        parsed = result.get("parsed")
        if parsed:
            try:
                raw_text = json.dumps(parsed, ensure_ascii=False, indent=2, default=str)
                raw_lang = "json"
                break
            except Exception:
                pass
        if stdout and isinstance(stdout, str) and stdout.strip():
            raw_text = stdout
            raw_lang = "yaml" if any(c in stdout[:200] for c in (":", "-")) else "text"
            break
    if not raw_text:
        return message
    # 太长截到 12K 字符（用户 UI 上能 scroll 看完；超长还可以让用户去 admin UI 查 trace）
    if len(raw_text) > 12_000:
        raw_text = raw_text[:12_000] + "\n# ...（更多内容已截断，完整原文在 admin UI → 调用审计中可看）"
    return (
        message.rstrip()
        + "\n\n---\n\n"
        + "## 📋 完整原始配置（平台自动补全——模型未单独贴出来）\n\n"
        + f"```{raw_lang}\n{raw_text}\n```"
    )


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
   - swarm_query(verb=ps) 的 Node 列 / kube_query(verb=get,resource=pods,output=json) 的 spec.nodeName
     → 当作 zabbix host_query。
   - 看到 OOMKilled / exit 137 / Evicted / no space / DiskPressure / connection refused
     这类关键词时，**必须**追加一次 zabbix_get_host_overview 或 zabbix_get_host_storage_overview。
2. 主机层信号 → 反向回到容器层。
   - 主机 CPU/MEM/DISK 告警时，确认这台 Node 上跑的服务是否同步异常
     （swarm_query(verb=ls) 看服务、kube_query(verb=get,resource=pods,selector=…) 看 pod）。
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
            "下表列出所有已接入的集群。**用户在对话里如果提到表中的触发关键词、"
            "别名、IP，你应当从对应行取 ``connection_id`` 作为 skill 的 "
            "``connection_id`` 参数传**。命中规则（优先级从高到低）：",
            "- ⭐ 用户文本里**精确命中触发关键词**（tags） → 走那条",
            "- 用户说出别名（中文/英文）或别名里的子串 → 走那条",
            "- 用户说出 manager IP / kubeconfig server IP / 节点 IP 前缀 → 走匹配那条",
            "- 用户提到的节点名（``worker2.chinasws.com`` / ``lowcode-master01``）"
            "  能在某条的 \"节点命名规律\" 里识别 → 走那条",
            "- 都没命中 → 走该 type 的 ``[默认]``；没默认就用列表第一条",
            "",
            "**关键：同一个触发关键词可能命中多条 connection**（同集群的 host_agent + "
            "swarm/k8s 通常打同样的关键词）。这是**逻辑集群**的设计——你需要哪个 type "
            "的 skill 就用同一关键词对应该 type 的 connection_id。",
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
                tags = c.get("tags") or []

                # 识别线索：根据 type 提取关键字段
                clue = self._connection_routing_clue(t, cfg)

                flag_tags = []
                if c.get("is_default"):
                    flag_tags.append("[默认]")
                if selected_connections.get(t) == cid:
                    flag_tags.append("[当前会话已选]")
                flag_str = " ".join(flag_tags)

                lines.append(
                    f"- **{alias}** {flag_str}  ←  ``connection_id={cid}``"
                )
                if tags:
                    keyword_str = ", ".join(f"``{t}``" for t in tags)
                    lines.append(f"  - 🏷️ **触发关键词**：{keyword_str}")
                lines.append(f"  - name=``{name}``，type=``{t}``")
                if clue:
                    lines.append(f"  - 识别线索：{clue}")
            lines.append("")

        lines.extend([
            "## 路由示例（few-shot）",
            "",
            "用户说「**bigdata6** 节点磁盘看一下」→ bigdata6 命中 `BigData Swarm` 的"
            "节点命名规律 → 调 ``host_query(node='bigdata6', command='df -h')`` 传该集群的 ``connection_id``。",
            "",
            "用户说「**codewave** 上 default 命名空间 pod 状态」→ codewave 命中 K8s "
            "集群的别名 → 调 ``kube_query(verb='get', resource='pods', namespace='default')`` 传该集群的 ``connection_id``。",
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

        # ---- 会话记忆：把历史对话拼进 messages（同会话多轮上下文）----
        # 设计：
        #   1) 取最近 ``CHAT_HISTORY_RECENT`` 条 user/assistant 消息原文
        #   2) 若历史 > ``CHAT_HISTORY_SUMMARIZE_AT`` 条，自动用模型把"摘要边界之前"
        #      的对话压成一段摘要（落库 role=summary），新轮次只带"最新摘要 + 近 N 条"
        #   3) 摘要表是 chat_message 里 role=summary 的伪行，admin UI 也能看
        history_msgs, summary_text = self._load_session_memory(
            session_id=session_id, user_message=user_message,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._load_system_prompt()},
            # 关键：把"当前可用集群"动态注入第二条 system 消息——让 LLM 能从用户
            # 口语里的别名 / IP / 节点名 推断要传哪个 connection_id 给 skill。
            {"role": "system", "content": self._build_cluster_registry_prompt(
                selected_connections=selected_connections or {},
            )},
        ]
        if summary_text:
            messages.append({
                "role": "system",
                "content": (
                    "## 会话历史摘要（早于以下原文部分）\n\n"
                    + summary_text
                    + "\n\n（以上是平台自动压缩的旧对话；下面是最近 N 轮原文。）"
                ),
            })
        # 历史原文（user / assistant 交替）
        messages.extend(history_msgs)

        # ---- 意图分类 + 输出模式动态注入（临门一脚）----
        # 纯靠 system prompt 描述模式，模型遵循度受 prompt 长度衰减影响。
        # 这里基于用户当前消息的关键词识别意图（B/C/D/F/G/H），用一条独立 system
        # 消息**重申**对应输出模式——这条紧贴当前 user message，比开头的 system 长篇
        # 更显眼，模型遵循率显著提升。
        intent_hint = _classify_intent_hint(user_message)
        if intent_hint:
            messages.append({"role": "system", "content": intent_hint})

        # 当前用户消息（保证总在最后）
        messages.append({"role": "user", "content": user_message})
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
                            message=_augment_message_for_intent(
                                summary.get("content") or "操作待用户确认。",
                                trace, user_message),
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
                        message=_augment_message_for_intent(
                            summary.get("content") or "已完成排查，但模型没有输出总结。",
                            trace, user_message),
                        trace=trace,
                        pending_actions=pending_actions,
                    )
                return AgentOutcome(
                    message=_augment_message_for_intent(
                        message.get("content") or "未获得明确结论。",
                        trace, user_message),
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
                    message=_augment_message_for_intent(
                        summary.get("content") or "操作待用户确认。",
                        trace, user_message),
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
            message=_augment_message_for_intent(
                summary.get("content") or "已完成排查，但模型没有输出总结。",
                trace, user_message),
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

    # ----- 会话记忆：加载历史 + 自动摘要 ----------------------------------- #

    def _load_session_memory(
        self,
        *,
        session_id: str | None,
        user_message: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """从 store 拉历史 + 必要时生成摘要。

        Returns:
            (history_messages, summary_text)
            - history_messages: 最近 N 条 user/assistant 原文，OpenAI 格式
            - summary_text: 摘要文本（如果有），插入到 system 消息里；没有就空串
        """
        if not session_id:
            return [], ""
        store = getattr(self.runtime, "store", None)
        if store is None or not hasattr(store, "list_chat_messages"):
            return [], ""

        try:
            total_count = store.count_chat_messages(session_id, exclude_summary=True)
            latest_summary = store.get_latest_chat_summary(session_id)
        except Exception as exc:
            logger.warning("load_session_memory: 读历史失败：%s", exc)
            return [], ""

        # 1) 决定要不要先生成新摘要
        summary_covers_until = 0
        if latest_summary:
            meta = latest_summary.get("metadata_json") or {}
            try:
                summary_covers_until = int(meta.get("covers_until_id") or 0)
            except (TypeError, ValueError):
                summary_covers_until = 0

        # "未摘要"条数 = 总条数（不含 summary） - 已被摘要覆盖到的位置
        # 这里是粗算：count 是当前未删的总条数；summary_covers_until 是消息 id，
        # 它们量纲不同。但只要 count > 阈值 + 上次摘要后增长很多，就再生成一次。
        if total_count > CHAT_HISTORY_SUMMARIZE_AT:
            # 看看自上次摘要后是否累积超过阈值
            # 简单策略：每超过阈值一次就生成一次摘要，覆盖到 当前总数 - CHAT_HISTORY_RECENT
            new_summary = self._summarize_old_messages(
                session_id=session_id, store=store,
                exclude_recent=CHAT_HISTORY_RECENT,
                prev_summary_text=(latest_summary or {}).get("content", ""),
            )
            if new_summary:
                latest_summary = new_summary

        # 2) 拉最近 N 条原文给 prompt 用
        try:
            rows = store.list_chat_messages(
                session_id, limit=CHAT_HISTORY_RECENT, exclude_summary=True,
            )
        except Exception as exc:
            logger.warning("load_session_memory: list_chat_messages 失败：%s", exc)
            rows = []

        history_msgs: list[dict[str, Any]] = []
        for row in rows:
            role = row.get("role")
            content = (row.get("content") or "").strip()
            if not content:
                continue
            # 只带 user / assistant 纯文本——tool_calls / tool 响应不带（避免撑爆 context
            # 且旧数据可能已过期）
            if role not in ("user", "assistant"):
                continue
            # 注意：当前轮 user 消息已经在 store 里了（chainlit 调 ask 前先 save）。
            # 不能让 history 里再出现一条跟 user_message 完全相同的——会变成"用户问了
            # 两次"。这里做去重：尾部如果是同样的 user content 就丢掉。
            if (
                history_msgs and role == "user"
                and content == user_message.strip()
                and row is rows[-1]
            ):
                continue
            history_msgs.append({"role": role, "content": content})

        # 极端兜底：list_chat_messages 返回的最后一条还是当前 user_message，再剥一次
        if (
            history_msgs
            and history_msgs[-1].get("role") == "user"
            and (history_msgs[-1].get("content") or "").strip() == user_message.strip()
        ):
            history_msgs.pop()

        summary_text = (latest_summary or {}).get("content", "") if latest_summary else ""
        return history_msgs, summary_text

    def _summarize_old_messages(
        self,
        *,
        session_id: str,
        store,
        exclude_recent: int,
        prev_summary_text: str,
    ) -> dict | None:
        """调模型把"早于最近 N 条的旧对话"压成摘要，落库 role=summary。

        失败不阻塞主流程——返回 None，调用方降级到"没摘要"。
        """
        try:
            # 拉全部（不含 summary），然后截掉最后 exclude_recent 条
            all_msgs = store.list_chat_messages(
                session_id, limit=10_000, exclude_summary=True,
            )
        except Exception as exc:
            logger.warning("summarize: list 全量失败：%s", exc)
            return None
        if len(all_msgs) <= exclude_recent:
            return None  # 没多少东西可摘要

        to_summarize = all_msgs[:-exclude_recent]
        if not to_summarize:
            return None

        # 拼"对话原文"喂给模型
        original_chunks: list[str] = []
        if prev_summary_text:
            original_chunks.append(f"[之前的摘要]\n{prev_summary_text}\n\n[之后的对话]")
        for m in to_summarize:
            role = m.get("role", "?")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            tag = {"user": "用户", "assistant": "助手"}.get(role, role)
            original_chunks.append(f"{tag}：{content}")
        original = "\n\n".join(original_chunks)
        # 控制摘要 prompt 总长度——模型再多也只能吃 30K char 左右；超过截断头部
        if len(original) > 30_000:
            original = "[早期更老的对话已省略]\n\n" + original[-29_000:]

        try:
            # 不传 tools——纯文本生成，跟主对话循环复用同一个 model client
            resp = self.model.create_completion(
                messages=[
                    {"role": "system", "content": _SUMMARY_PROMPT},
                    {"role": "user", "content": original},
                ],
            )
            summary_text = (resp.get("content") or "").strip()
        except Exception as exc:
            logger.warning("summarize: 模型调用失败：%s", exc)
            return None
        if not summary_text:
            return None

        # 落库 role=summary，metadata 记 covers_until_id（最后一条被摘要消息的 id）
        covers_until_id = int(to_summarize[-1].get("id") or 0)
        try:
            store.save_chat_message(
                session_id, "summary", summary_text,
                metadata={"covers_until_id": covers_until_id,
                          "summarized_count": len(to_summarize)},
            )
        except Exception as exc:
            logger.warning("summarize: 落库失败：%s", exc)
            return None
        return {
            "role": "summary",
            "content": summary_text,
            "metadata_json": {"covers_until_id": covers_until_id,
                              "summarized_count": len(to_summarize)},
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
            # 重构后没有 swarm_check_service_health；改走 swarm_query inspect 作为入口诊断
            return "swarm_query", {"category": "service", "verb": "inspect",
                                   "name": service_match.group(1)}

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
