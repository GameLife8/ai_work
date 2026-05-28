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
#
# 已知局限 / 未来升级路径
# -----------------------
# 关键词匹配天然脆——用户表达千变万化:
#   - "看下都有啥" 不命中 list_state（漏 "列" / "有哪些"）
#   - "瞧瞧 CPU 飙到哪了" 不命中 monitor 也不命中 diagnose
#   - 表达 "为啥服务 X 跑不起来" vs "为什么 X 跑不起来" 全靠 "为什么 / 起不来" 撞中
#
# **当前缓解措施**:
#   1. 关键词尽量覆盖常见变体（见下面的 list_state / monitor 里"显示/打印/瞧"类）
#   2. 兜底走 ``None`` 意图 → full tool set,不会因为分类错失能力（只损失输出模式提示）
#   3. ``_filter_tools_by_intent`` 过滤后 < 2 个 tool 时自动回退 full set
#
# **未来升级**（需要单独 1-2 天专项,不在本期 Medium 范围）:
#   - 用小型 embedding 模型（如 BGE-small-zh / m3e-small）算用户消息和各意图
#     canonical phrase 的相似度,top-1 作为意图
#   - 或者用主模型自己做一轮短分类（额外 100-200 token,但准确率显著提升）
#   - 落地时建议在 ModelManager 里允许配 "auxiliary_model_id" 跑分类任务,主模型
#     专心干 tool calling

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
        # 高频变体:
        "飙到", "打满", "占用率", "使用率", "吃满", "爆了", "高吗",
        "性能怎么样", "压力大不大",
    ],
    # C 类：列表 / 状态总览
    "list_state": [
        "列一下", "列出", "都有哪些", "有哪些", "多少个",
        "全部服务", "全部 pod", "全部 deployment", "所有",
        "状态总览", "概况", "概览",
        # 用户高频变体（audit 漏掉的）：
        "显示一下", "显示下", "打印一下", "打印下", "看下都有啥", "都有啥",
        "都跑着啥", "跑了什么", "跑了哪些", "在跑啥",
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
        "为什么", "为啥", "起不来", "启动失败", "失败", "异常", "报错", "出错",
        "crashloop", "CrashLoop", "OOM", "oomkilled", "evicted",
        "连不上", "不通", "丢包", "502", "504", "卡顿", "慢",
        "排查", "诊断", "分析下", "看看怎么回事",
        # 高频变体:
        "怎么回事", "啥情况", "什么情况", "咋回事", "为何", "查一查",
    ],
}


# B 类（查看/配置）专用：verb + noun 双命中（任意 verb 任意 noun 同时出现就命中）
#
# 因为中文里用户经常分开说："配置可以展示一下" / "展示一下配置" / "把配置给我看看" /
# "k8s 的 coredns ConfigMap 看下"——单关键词"展示配置"按字面顺序匹配只能覆盖一种。
# 拆成动词组 + 名词组，**只要句中同时有一个动词和一个名词就命中**，覆盖率高得多。
_CONFIG_VIEW_VERBS = (
    # 显式"展示/查看"动词
    "展示", "看一下", "看下", "看看", "瞧一下", "给我看", "拿出来",
    "贴一下", "贴出来", "出示", "显示", "查看", "查一下", "show", "查询配置",
    # "解释/说明" 类——用户想看 + 想懂，理应贴 yaml 再解释
    "解释", "说明", "讲解", "说说", "聊聊", "介绍", "讲讲",
    # "是什么 / 长什么样" 等疑问句式——本质也是"展示+解释"
    "是什么", "长什么样", "是怎么", "怎么样的", "啥样", "什么样",
    "告诉我", "了解一下", "想知道", "学习一下",
    # 单字"看" 与单字"贴"太宽，不放——靠"看一下/看下/看看"已覆盖
)
_CONFIG_VIEW_NOUNS = (
    "配置", "config", "yaml", "json", "原文", "原始", "raw", "spec",
    "configmap", "Secret", "secret",
    "环境变量", "挂载", "镜像", "Corefile", "manifest",
    "stack", "compose", "Deployment yaml", "deploy yaml",
    # 用户常用"配置文件 / 配置内容 / 配置详情"等组合
    "配置文件", "配置内容", "配置详情",
)


# ----- 意图 → 相关 skill 子集 -------------------------------------------- #
# tools schema 是 prompt 最大头（~5K tokens / 56%）。每次 27 个 skill 全传给模型
# 大多数浪费——用户问"看配置"用不到 scale/rollback；用户问"重启服务"用不到 metric。
#
# 按意图过滤后能省 ~3.5K tokens / 次（−14%）。失败回退：如果模型空手回了（既没
# 调 tool 也没给文本），自动重试一次 full tool set。
#
# 设计原则（很重要，改这里前先读一遍）：
#   1. **审批是平台正交关注点，不是意图的过滤维度**。
#      ``host_run_command`` / ``host_run_command_async`` 这些走 admin 审批的逃生口
#      该出现在哪个意图里就出现在哪个意图里——平台的 ``needs_confirmation`` 机制
#      会接管确认流程；admin/user 权限由 ``SkillRegistry.list(visibility=...)``
#      在上一层处理。意图层**不要**为了"审批麻烦"而剔除这些 skill，否则模型遇到
#      ``host_query`` binary 白名单覆盖不到的命令（``docker ps`` / ``virsh list``
#      等）就只能写"请联系 admin"的死信，体验非常差。
#   2. **每个意图都应该是该类工作的完整工具箱**：典型只读 +（必要时）逃生口 +
#      上下文相关的辅助 skill + runbook 入口。让模型选最合适的，不要让模型因为
#      意图过滤"无米下锅"。
#   3. **写操作意图天然带 read query**——写之前要先看状态确认目标。
#   4. **chat_intro / knowledge** 是纯路由/引导意图，不真正取证，给 runbook 入口
#      就够。
#   5. **diagnose / 未知意图** 走全集 —— 跨层追根因的场景过滤掉任何 skill 都可能误伤。
_INTENT_TOOLS: dict[str | None, list[str] | None] = {
    "config_view": [
        # 通用查询三把口
        "kube_query", "swarm_query", "host_query",
        # 集群一把口概览（看配置时常需要先定位节点 / 服务）
        "swarm_cluster_overview",
        # 日志也是"看原文"的一种
        "k8s_get_pod_logs",
        # 容器网络 namespace 排障（看路由/iptables 配置）
        "host_inspect_container_netns",
        # 逃生口：``docker inspect`` / ``cat`` 大配置 / ``virsh dumpxml`` 等
        # host_query binary 白名单覆盖不到的"看原文"场景。需 admin 审批，但
        # 这是审批的职责不是意图过滤的职责。
        "host_run_command", "host_run_command_async",
        # 引导
        "platform_get_runbooks", "platform_run_runbook",
    ],
    "list_state": [
        # 通用查询三把口（覆盖大多数列表场景：kube get / swarm service ls / df / ps）
        "kube_query", "swarm_query", "host_query",
        # 集群级一把口（节点 + 服务 + 监控）
        "swarm_cluster_overview",
        # 异步任务列表
        "host_list_nodes", "host_list_tasks", "host_check_task",
        # 逃生口：``docker ps`` / ``docker stats`` / ``virsh list`` 等
        # 这是用户"我这台主机跑了哪些容器"最常踩的坑——
        # host_query 的 binary 白名单故意不放 docker（隔离到 admin 审批），
        # 没有这两把口模型就只能放弃取证。
        "host_run_command", "host_run_command_async",
        # 引导
        "platform_get_runbooks", "platform_run_runbook",
    ],
    "monitor": [
        # Zabbix / 指标主线
        "zabbix_get_host_overview", "zabbix_get_host_storage_overview",
        "metric_query_peak", "metric_query_window_around",
        # 一把口拉全集群监控（巡检场景必备）
        "swarm_cluster_overview",
        # 快速宿主机指标（vmstat / iostat / free / uptime / ps top）
        "host_query",
        # 跨层定位："这台 node 上跑了什么服务/pod"
        "swarm_query", "kube_query",
        # 长采样（``sar -A 1 60`` / ``iostat -x 1 30``）+ 抓包
        "host_run_command_async", "host_capture_packets",
        # 内核 events——OOM / softlockup / call trace 等性能事件
        "host_kernel_events",
        # 引导
        "platform_get_runbooks", "platform_run_runbook",
    ],
    "write_action": [
        # 全部写操作 skill
        "k8s_scale_deployment", "k8s_restart_deployment", "k8s_rollout_undo",
        "swarm_scale_service", "swarm_update_service_image",
        "swarm_rollback_service", "swarm_remove_service", "swarm_force_update_service",
        # 逃生口（写）+ 抓包
        "host_run_command", "host_run_command_async", "host_capture_packets",
        # 写之前要先查状态确认目标
        "kube_query", "swarm_query", "host_query", "swarm_cluster_overview",
        # 写完验证：日志
        "k8s_get_pod_logs",
        # 也允许直接走 runbook（含写动作的 runbook）
        "platform_get_runbooks", "platform_run_runbook",
    ],
    "async_task": [
        # 核心异步循环
        "host_run_command_async", "host_check_task", "host_list_tasks",
        # 长抓包本身就是异步任务
        "host_capture_packets",
        # 提交前查一下在哪台 node 跑
        "host_query", "kube_query", "swarm_query",
        "host_list_nodes", "swarm_cluster_overview",
        # 引导（部分 runbook 含长任务步骤）
        "platform_get_runbooks", "platform_run_runbook",
    ],
    "knowledge": [
        # 知识 / 引导类——只需要 runbook 入口
        "platform_get_runbooks", "platform_run_runbook",
    ],
    "chat_intro": [
        # 闲聊 / 自介——模型通常会纯文本回复，给 2 个 runbook 入口意思一下
        "platform_get_runbooks", "platform_run_runbook",
    ],
    "diagnose": [
        # 诊断意图保守一点——可能跨域追根因，全集（不过滤）
        # 用 None 标记，调用方知道走 full set
    ],
    None: [
        # 未识别意图：走 full set，避免漏 tool
    ],
}


def _filter_tools_by_intent(
    full_tools: list[dict],
    intent: str | None,
) -> tuple[list[dict], bool]:
    """根据意图过滤 tools。返回 (过滤后的 tools, 是否被过滤过)。

    diagnose / unknown / chat_intro 等不过滤的意图：返回 full_tools + False。
    """
    whitelist = _INTENT_TOOLS.get(intent)
    if not whitelist:
        return full_tools, False
    allowed = set(whitelist)
    filtered = [t for t in full_tools if t.get("function", {}).get("name") in allowed]
    # 如果过滤后 < 2 个工具，安全起见走 full set（防意图识别错导致模型无 tool 用）
    if len(filtered) < 2:
        return full_tools, False
    return filtered, True


_INTENT_HINTS: dict[str, str] = {
    "config_view": (
        "🎯 **本次用户意图：查看 / 配置 / 看原文（B 类）**\n"
        "用户要的是真实原始数据，不是你的总结。**严禁**跳过原文直接概括「该服务是 X，运行健康……」。\n"
        "\n"
        "**必须按这个格式输出**：\n"
        "1. **先用 ``` 代码块（``` yaml / ``` json）原样贴 skill 返回的原文**——\n"
        "   取 ``result.parsed`` 或 ``result.stdout``。完整、不重排、不省字段、不折叠。\n"
        "   超长（> 200 行）按段贴：先关键段（image/replicas/networks/labels/placement/"
        "healthcheck/env/mounts），告诉用户「完整原文在 trace 里」。\n"
        "2. **再用 1–3 段中文逐项翻译**关键配置：image 是什么 / replicas 几个 / "
        "networks 接哪个 overlay / placement 约束什么意思 / labels 起的作用 / 健康检查策略。\n"
        "3. 最后**可选**一句运维建议：「X 字段建议改 Y，避免 Z」。"
    ),
    "list_state": (
        "🎯 **本次用户意图:列表 / 状态总览(C 类)—— 先 raw 后解读**\n"
        "\n"
        "**铁律:能贴的原始输出先贴,再做简短解读。严禁把 raw 数据「加工」成中文段落丢失原文。**\n"
        "\n"
        "**必须按这个顺序输出**:\n"
        "1. **第一段**:原样贴 skill 返回的 raw 输出\n"
        "   - 命令输出(``docker ps`` / ``ss -ltnp`` / ``ps -ef`` 等)→ ``` 代码块包裹原文\n"
        "   - JSON / 结构化数据(``kubectl get -o json`` 的 items)→ 转 markdown 表格\n"
        "   - 字段挑关键的(名字 / 状态 / 镜像 / 副本 / 重启次数 / 创建时间 / Node / 端口)\n"
        "2. **第二段**(1-3 句解读):有多少个,几个异常,哪些值得关注\n"
        "3. 异常项行首加 ⚠️ 或 🚨;行末一句简短原因\n"
        "4. 异常存在时**主动追问**:「想详细看哪一个? 我可以拉它的 describe / 日志」\n"
        "\n"
        "**反例**(严禁):用户问「跑了哪些容器」,你只写「共 28 个容器,1 个 ai-ops-agent...」 \n"
        "—— 你**丢了** ``docker ps`` 的 NAMES/IMAGE/STATUS/PORTS 表格,用户没法看到具体每个容器。"
    ),
    "monitor": (
        "🎯 **本次用户意图：资源 / 性能监控（D 类）—— 数据卡片 + 解读**\n"
        "**严禁**只贴一大段文字描述。\n"
        "\n"
        "**必须按这个格式输出**：\n"
        "1. markdown 表格或卡片展示当前值 + 阈值对比：\n"
        "   ```\n"
        "   | 指标          | 当前值  | 阈值   | 状态  |\n"
        "   | CPU           | 78%     | 80%    | ✅    |\n"
        "   | Memory        | 94%     | 85%    | ⚠️    |\n"
        "   | /var          | 92%     | 80%    | 🚨    |\n"
        "   ```\n"
        "2. 1–2 段解读：哪个指标接近/超阈，趋势怎样，可能影响什么。\n"
        "3. 一句下一步建议（看更细的 skill / 扩容 / 清盘 / 排查应用）。"
    ),
    "write_action": (
        "🎯 **本次用户意图：执行写操作（E 类）—— needs_confirmation 流程**\n"
        "调用对应写 skill 后会返回 ``status=needs_confirmation``。**禁止再调任何 tool**。\n"
        "\n"
        "**必须按这个格式输出**（不是五段式）：\n"
        "**我打算执行**：``skill_name(args)``\n"
        "**原因**：基于刚才取证的 X 证据，做这个操作能 Y。\n"
        "**影响范围**：会影响 X 服务的 Y 个副本，预计 Z 秒内完成。\n"
        "**回滚方式**：如果出问题，调用 ``rollback_skill`` 可恢复。\n"
        "请在**下方点击「确认」或「取消」**。\n"
        "（如果是 ``requires_admin_approval``，明确说「**需 admin 审批**」）"
    ),
    "async_task": (
        "🎯 **本次用户意图：异步长任务（F 类）—— 提交回执 + 轮询提示**\n"
        "估计 > 30s 的命令一律用 ``host_run_command_async``。\n"
        "\n"
        "**提交时**：\n"
        "```\n"
        "已提交异步任务：\n"
        "- task_id: ptk_xxx\n"
        "- node: bigdata6\n"
        "- 命令: du -sh /var/log/*\n"
        "- 最大运行时间: 600s\n"
        "预计 1–5 分钟跑完，你说「看任务结果」我去 host_check_task 取。\n"
        "```\n"
        "**查询任务结果时**：\n"
        "```\n"
        "任务 ptk_xxx 已完成（耗时 X 秒），结果如下：\n"
        "（贴 stdout 代码块）\n"
        "```"
    ),
    "knowledge": (
        "🎯 **本次用户意图：知识 / 引导（G 类）—— 结构化清单**\n"
        "**必须按这个格式输出**：\n"
        "1. 简短引言（1 句）：本场景的核心思路是什么。\n"
        "2. 步骤列表（有序号）：每步对应一个 skill 调用 + 用途说明。\n"
        "3. 关键注意点（项目符号）：避坑 / 优先级 / 替代方案。\n"
        "4. 如有现成 runbook，明确告诉用户「直接调 ``platform_run_runbook(user_query=...)`` 一键跑」。"
    ),
    "chat_intro": (
        "🎯 **本次用户意图：闲聊 / 自介（H 类）—— 简短自介 + 引导**\n"
        "**必须按这个格式输出**：\n"
        "1. 一句话自我介绍。\n"
        "2. 列 4–6 个典型能力分类（诊断 / 看配置 / 查状态 / 监控 / 执行操作 / 长任务）。\n"
        "3. 邀请用户给出具体场景：「你想从哪个集群/服务/节点开始？」\n"
        "**禁止**：堆 skill 列表、长篇大论、客套话。"
    ),
    "diagnose": (
        "🎯 **本次用户意图：诊断 / 排障（A 类）—— 五段式中文报告**\n"
        "**必须按这个格式输出**：\n"
        "**当前状态**：服务/Pod/主机现在是健康还是异常，关键数字（副本数/重启次数/磁盘 %）。\n"
        "**检测过程**：你按什么顺序查了哪些 skill，原因是什么。\n"
        "**关键证据**：1–3 条原始信息（错误码、日志片段、metric 数字），用 ``` 代码块引用原文。\n"
        "**判断结论**：根因是什么；没法 100% 确认就写「高度疑似 X + 备选可能性 Y」。\n"
        "**建议操作**：具体可执行——扩 N 副本 / 重启 X / 清 /var/log。"
        "如果建议是写操作，明确告诉用户「我可以帮你执行 ``skill_name``，请下方点击确认」。"
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


def _is_data_query_trace_item(item: dict) -> bool:
    """trace 里这一条是不是「拉数据」型只读调用?

    通用化:任何 host/swarm/kube/swarm_cluster_overview 的成功只读调用都算。
    """
    if item.get("status") != "ok":
        return False
    tool = item.get("tool_name", "")
    if tool in ("kube_query", "swarm_query", "host_query",
                "swarm_cluster_overview",
                "zabbix_get_host_overview", "zabbix_get_host_storage_overview"):
        return True
    return False


def _message_already_has_raw(message: str) -> bool:
    """模型自己已经贴了原文(code block 或 markdown 表格) → 平台不重复贴。

    markdown 表格判定:有 ``| --- |`` 这种分隔行(table separator),
    必须严格,避免把 ``|`` 当 OR 用的普通文本误判成表格。
    """
    if not message:
        return True
    if "```" in message:
        return True
    # markdown table separator 行:开头是 `|`,主体是 `-` 和 `|`
    import re
    if re.search(r"^\s*\|[\s\-:|]+\|\s*$", message, re.M):
        return True
    return False


def _extract_raw_for_display(item: dict) -> tuple[str, str]:
    """从 trace item 抽可直接展示的 raw 文本,返回 (text, lang)。空 raw 返回 ("","" )。

    优先级:
      1. swarm_query 反推的 ``compose_yaml`` —— 用户最熟悉的格式
      2. ``items`` / ``nodes`` / 其它简单 list 字段 —— 转 JSON
      3. ``stdout`` —— 命令原文
      4. ``parsed`` —— JSON
    """
    result = item.get("tool_result") or {}
    if not isinstance(result, dict):
        return "", ""

    compose_yaml = result.get("compose_yaml")
    if compose_yaml and isinstance(compose_yaml, str) and compose_yaml.strip():
        return compose_yaml, "yaml"

    stdout = result.get("stdout") or ""
    if stdout and isinstance(stdout, str) and stdout.strip():
        # 推语言:看头部有没有 YAML 风格冒号
        head = stdout[:200]
        if ":" in head and "\n" in head and any(c in head for c in ("-", " ")):
            return stdout, "yaml"
        return stdout, ""    # 留空让 chainlit 按默认渲染(更像 ``docker ps`` 表格)

    parsed = result.get("parsed")
    if parsed not in (None, [], {}):
        try:
            return json.dumps(parsed, ensure_ascii=False, indent=2, default=str), "json"
        except Exception:
            pass

    return "", ""


def _augment_message_for_intent(message: str, trace: list[dict], user_message: str) -> str:
    """**平台保证机制**:能展示 raw 数据就先展示,不能展示的才让模型出报告。

    设计哲学(对应用户反馈"能展示的优先展示输出结果"):
      不再依赖意图分类(关键词太脆弱——"iiot" 撞 "io"、"在跑"漏 list_state...
      用户也明确说不要再扩词典污染 prompt)。改用 **trace 事实判定**:

    触发条件(全部满足):
      1. trace 里至少有一个"拉数据"型成功调用(host/swarm/kube/zabbix 等)
      2. trace 里没有 ``needs_confirmation`` 状态的写操作(那是审批说明场景,
         raw 不该冲走模型写的"我打算执行...请确认"那段话)
      3. 模型生成的 message **还没**自己贴 raw(没 ``` code block 也没 markdown
         表格分隔行 ``| --- |``)

    满足 → 从 trace 抽最近一条 raw 输出拼到 message 末尾,用 ``` 包裹。

    用户原话:
      > "匹配用户的关键词感觉还是太差了,关键词扩展我感觉就不要了"
      → 所以不能再靠 _classify_intent 做判定
    """
    if not message:
        return message

    # 模型自己贴 raw 了 → 不重复
    if _message_already_has_raw(message):
        return message

    # 写操作 needs_confirmation 场景 → 模型在写"我打算执行...请确认"那段
    # 应该被原样呈现,raw 拼上来会喧宾夺主。同时确认卡片在下方独立显示。
    for item in (trace or []):
        if item.get("status") == "needs_confirmation":
            return message
        if item.get("pending_token"):
            return message

    # 找最近一条"拉数据"成功调用
    data_item = None
    for item in reversed(trace or []):
        if _is_data_query_trace_item(item):
            data_item = item
            break
    if data_item is None:
        return message

    raw_text, raw_lang = _extract_raw_for_display(data_item)
    if not raw_text:
        return message

    # 截 12K 字符防 UI 撑爆;完整原文在 admin UI → 调用审计可查
    if len(raw_text) > 12_000:
        raw_text = (
            raw_text[:12_000]
            + "\n# …(更多内容已截断,完整原文在 admin UI → 调用审计可查)"
        )

    fence_lang = raw_lang if raw_lang else ""
    return (
        message.rstrip()
        + "\n\n---\n\n"
        + "### 📋 原始数据\n\n"
        + f"```{fence_lang}\n{raw_text}\n```"
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


def _parse_tool_call_args(tool_call: dict) -> tuple[dict | None, dict | None]:
    """解析 ``tool_call.function.arguments``，把 JSON 失败转成 error envelope。

    国产模型偶发会输出截断的 JSON / 带中文引号的 JSON / 顶层不是 object 的合法 JSON
    （传成 list 或 string）。这些情况下直接 ``json.loads`` 会抛 JSONDecodeError 把
    整个 ``ask()`` 中断，用户看不到任何反馈、已消耗的 token 全浪费。

    本函数把"硬抛错"转成"软错误"——返回 error envelope 让 agent 回灌给模型，模型
    下一轮能基于这个错误自纠正。

    Returns:
        ``(args, None)`` 解析成功；
        ``(None, error_envelope)`` 解析失败，error_envelope 应当回灌给模型。
    """
    fn = tool_call.get("function", {})
    name = fn.get("name")
    raw_args = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        return None, {
            "skill": name,
            "status": "error",
            "error": f"参数 JSON 解析失败：{exc}",
            "result": {
                "_hint": "请重新调用该工具，确保 arguments 是合法的 JSON object。"
                         "如果含中文，注意必须用 ASCII 双引号包裹键值。",
                "raw_arguments_preview": str(raw_args)[:200],
            },
        }
    if not isinstance(args, dict):
        return None, {
            "skill": name,
            "status": "error",
            "error": f"arguments 顶层必须是 JSON object，实际是 {type(args).__name__}",
            "result": {
                "_hint": "请重新调用该工具，arguments 必须是 {\"key\": \"value\"} 形式的 object。",
                "raw_arguments_preview": str(raw_args)[:200],
            },
        }
    return args, None


@dataclass
class AgentOutcome:
    message: str
    trace: list[dict[str, Any]]
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    # Token usage：本次 ask() 内所有 LLM 调用的累计消耗。
    # 结构：{"prompt_tokens": int, "completion_tokens": int, "total_tokens": int,
    #        "calls": int, "model": str（首个 model 名）}
    # 由 chainlit_app / API 入口持久化（写入 chat_message.metadata.usage 或
    # 单独的 platform_token_usage 表）。空 dict 表示模型未返回 usage 字段（部分
    # 国产模型早期版本可能漏返）。
    usage: dict[str, Any] = field(default_factory=dict)


def _accumulate_usage(total: dict[str, Any], step_usage: dict[str, Any]) -> None:
    """把单步 usage 累加到 total。原位修改 total。

    Args:
        total: ``AgentOutcome.usage`` 的累加 dict
        step_usage: ``message["_usage"]`` 取出来的单步消耗
    """
    if not step_usage:
        return
    total["prompt_tokens"] = total.get("prompt_tokens", 0) + step_usage.get("prompt_tokens", 0)
    total["completion_tokens"] = total.get("completion_tokens", 0) + step_usage.get("completion_tokens", 0)
    total["total_tokens"] = total.get("total_tokens", 0) + step_usage.get("total_tokens", 0)
    total["calls"] = total.get("calls", 0) + 1
    # model 取首次出现的（同会话中途切模型场景极少）
    total.setdefault("model", step_usage.get("model", "unknown"))


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

        每次 ask() 都重新组装一次——admin 改完 prompt 通常在 ≤5s 内对下一次
        会话生效（受 ``runtime.prompt_segments_cache`` TTL 影响）。

        历史 perf 槽点：25 个并发 session 每秒打 25 次 ``list_prompt_segments``,
        DB 没必要扛。runtime 上挂了 TTL 缓存（默认 5s）,hit 率高且不损失运营体验。
        """
        store = self.runtime.store
        cache = getattr(self.runtime, "prompt_segments_cache", None)

        def _load() -> list:
            try:
                if not hasattr(store, "list_prompt_segments"):
                    return []
                return store.list_prompt_segments() or []
            except Exception:
                return []

        records = cache.get(_load) if cache is not None else _load()
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
        # 走 runtime 的 TTL cache 避免每次 ask() 都打 DB（25 并发 ×/秒）。
        # ``list()`` 内部已经 enabled 过滤,这里再二次过滤主要为兼容 cache 返回。
        cache = getattr(self.runtime, "connections_list_cache", None)
        try:
            if cache is not None:
                conns = cache.get(lambda: self.runtime.connection_manager.list())
            else:
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
        # ⭐ 集群关键字自动路由 —— 按 connection.tags 子串匹配 user_message
        # ====================================================================
        # 解决:用户说"bigdata-swarm 巡检"时,平台不能傻乎乎走默认 connection。
        # 把 user_message 跟每个 enabled connection 的 ``tags`` 做子串匹配,
        # 每个 type 选命中 tag 数最多的胜出,塞进 ``selected_connections``。
        # SkillContext.connection_for(type, None) 会优先用 selected,无命中再走 default。
        #
        # 设计要点:
        # - 完全基于 admin 给 connection 打的 ``tags`` —— 这是 single source of truth,
        #   不在代码里写死任何集群别名/IP。新增集群只要在后台打 tag,平台自动路由。
        # - 用户在 chainlit ChatSettings 里**显式选过的 connection 优先级最高**,
        #   不被自动路由覆盖(``setdefault`` 语义)。
        # - 与现有的 ``_build_cluster_registry_prompt`` 互补:那个是给 LLM 看的指引
        #   (走 tool loop 时让模型自己挑 connection_id);自动路由是给"模型不参与
        #   决策"的路径(预路由 runbook / signal-driven fallback)兜底。
        # ====================================================================
        sel = dict(selected_connections or {})
        for type_code, cid in self._resolve_clusters_from_query(user_message).items():
            sel.setdefault(type_code, cid)

        ctx = SkillContext(
            runtime=self.runtime,
            user=user,
            session_id=session_id,
            selected_connections=sel,
        )

        # ⭐ Runbook 强路由 —— 标准化流程绕开模型 tool loop
        # ====================================================================
        # 国产模型对"看到巡检/体检关键词主动调 platform_run_runbook"的指令遵循
        # 率很低,常常自己拆 swarm_query + zabbix 拼来拼去,踩 hostname/IP 不匹配
        # 等老坑。
        #
        # 解决方案:平台层做关键词预匹配,用户消息命中某个 enabled runbook 的
        # triggers 时,**直接绕过模型决策**,强制走 platform_run_runbook。
        # 模型只参与最终报告生成(在 runbook 内部),不参与"走不走 runbook"。
        # ====================================================================
        pre = self._maybe_runbook_preroute(user_message, ctx)
        if pre is not None:
            return pre

        visibility = "user" if user and user.get("role") != "admin" else None
        full_tools = self.registry.openai_tools(visibility=visibility)

        # ---- 意图分类（提前到 tools 过滤之前）----
        intent = _classify_intent(user_message)

        # ---- tools 按意图动态裁剪（省 ~3.5K tokens / 次）----
        # 失败回退：若 LLM 第一轮空手回（既没调 tool 也没给文本），下一轮自动用 full set 重试。
        tools, was_filtered = _filter_tools_by_intent(full_tools, intent)
        if was_filtered:
            logger.debug(
                "intent=%s, tools filtered: %d → %d",
                intent, len(full_tools), len(tools),
            )

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

        # ---- 输出模式动态注入（intent 已在上面识别）----
        # 这条紧贴当前 user message，比开头的 system 长篇更显眼，遵循率高。
        intent_hint = _INTENT_HINTS.get(intent) if intent else None
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
        # Token usage 累计：每次 create_completion 后从 message["_usage"] 累加；
        # ask() 结束时塞到 AgentOutcome.usage 给入口层（chainlit_app / API）持久化。
        total_usage: dict[str, Any] = {}

        for step in range(self.max_steps):
            # ⏬ 调用 LLM 前先压缩 messages，防止 8 步循环里上下文越积越多撑爆窗口
            messages = _maybe_compress(messages)

            message = self.model.create_completion(
                messages=messages, tools=tools, tool_choice="auto",
            )
            _accumulate_usage(total_usage, message.pop("_usage", {}))
            tool_calls = message.get("tool_calls") or []
            content = (message.get("content") or "").strip()

            # 空手回退：第一轮 LLM 既没调 tool 也没给文本——可能是 tools 被过滤掉
            # 了它想要的那个。重新跑一次给它 full set。
            if step == 0 and was_filtered and not tool_calls and not content:
                logger.info(
                    "intent=%s 过滤后 LLM 空手回，回退到 full tool set",
                    intent,
                )
                tools = full_tools
                was_filtered = False
                message = self.model.create_completion(
                    messages=messages, tools=tools, tool_choice="auto",
                )
                _accumulate_usage(total_usage, message.pop("_usage", {}))
                tool_calls = message.get("tool_calls") or []
                content = (message.get("content") or "").strip()

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
                        # 进入"待确认"路径——同主流程,只写 2-3 句精炼提议,
                        # 不重写卡片内容,确保按钮始终在屏幕内可见
                        summary = self.model.create_completion(
                            messages=messages + [{
                                "role": "system",
                                "content": (
                                    f"平台已根据 {reason} 自动调用 ``{name}``,返回 needs_confirmation。\n"
                                    "**chainlit 下方会自动弹确认卡片(✅/❌ 按钮)——你不要重写 skill/参数/有效期等卡片已有信息**。\n"
                                    "请用中文 2-3 句话:为什么平台触发了这个调用 / 主要风险点 / "
                                    "**结尾写**:`请在下方点击 ✅ 确认 或 ❌ 取消`。**禁止再调任何工具**。"
                                ),
                            }],
                        )
                        _accumulate_usage(total_usage, summary.pop("_usage", {}))
                        return AgentOutcome(
                            message=_augment_message_for_intent(
                                summary.get("content") or "请在下方点击 ✅ 确认 或 ❌ 取消。",
                                trace, user_message),
                            trace=trace,
                            pending_actions=pending_actions,
                            usage=total_usage,
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
                    _accumulate_usage(total_usage, summary.pop("_usage", {}))
                    return AgentOutcome(
                        message=_augment_message_for_intent(
                            summary.get("content") or "已完成排查，但模型没有输出总结。",
                            trace, user_message),
                        trace=trace,
                        pending_actions=pending_actions,
                        usage=total_usage,
                    )
                return AgentOutcome(
                    message=_augment_message_for_intent(
                        message.get("content") or "未获得明确结论。",
                        trace, user_message),
                    trace=trace,
                    pending_actions=pending_actions,
                    usage=total_usage,
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
                # JSON 解析交给 _parse_tool_call_args（含截断 JSON / 顶层非 object 等
                # 国产模型偶发输出的容错）；失败时回灌 error envelope 让模型自纠正。
                args, err_envelope = _parse_tool_call_args(tool_call)
                if err_envelope is not None:
                    logger.warning(
                        "model %s 返回非法 tool_call arguments；已回灌 error 让模型重试",
                        name,
                    )
                    trace.append(self._to_trace_item(name, {}, err_envelope))
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": self.invoker.serialize_for_model(err_envelope),
                    })
                    continue   # 跳过这次 tool_call，继续处理同轮的其他 tool_calls
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
                # 写操作已挂起:让模型给一段**精炼**的「为什么 + 风险」,**不要重写卡片内容**
                # ----------------------------------------------------------------
                # 平台 chainlit 已经会在下方独立卡片里把 skill / 参数 / 接入 / 有效期
                # 全部展示。如果让模型再写一遍"执行内容 / 影响范围 / 回滚方式",会:
                #   1. 拖长消息把卡片按钮挤到屏幕外,用户找不到按钮就超时(实测踩坑)
                #   2. token 浪费
                #   3. 信息冗余,用户反而看花眼
                #
                # 改成只让模型写"基于我刚才的取证,为什么要做这个操作 / 主要风险点",
                # 卡片内容由 chainlit 自己呈现。
                summary = self.model.create_completion(
                    messages=messages + [{
                        "role": "system",
                        "content": (
                            "上面工具结果中有 ``_pending=True`` 的写操作待确认。\n"
                            "**chainlit 下方会自动弹一张包含 skill / 参数 / 有效期的确认卡片,带 ✅ / ❌ 按钮——"
                            "你不要重写这些信息**。\n"
                            "\n"
                            "你只需要用**中文 2-3 句话**给出:\n"
                            "  - 基于刚才取证发现了什么(1 句)\n"
                            "  - 主要风险或注意事项(1 句,可选)\n"
                            "\n"
                            "结尾**必须**写一句:`请在下方点击 ✅ 确认 或 ❌ 取消`(让用户立刻意识到按钮在下方)。\n"
                            "\n"
                            "**禁止再次调用任何工具**。"
                        ),
                    }],
                )
                _accumulate_usage(total_usage, summary.pop("_usage", {}))
                return AgentOutcome(
                    message=_augment_message_for_intent(
                        summary.get("content") or "请在下方点击 ✅ 确认 或 ❌ 取消。",
                        trace, user_message),
                    trace=trace,
                    pending_actions=pending_actions,
                    usage=total_usage,
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
        _accumulate_usage(total_usage, summary.pop("_usage", {}))
        return AgentOutcome(
            message=_augment_message_for_intent(
                summary.get("content") or "已完成排查，但模型没有输出总结。",
                trace, user_message),
            trace=trace,
            pending_actions=pending_actions,
            usage=total_usage,
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

    def _resolve_clusters_from_query(
        self,
        user_message: str,
    ) -> dict[str, str]:
        """按 ``connection.tags`` 子串匹配 user_message,推断每种 type 走哪条 connection。

        Returns:
            ``{type_code: connection_id}`` —— 只包含**真有匹配**的 type。
            没匹配的 type 不写入,让调用方走平台 default。

        匹配规则
        --------
        对每个 enabled connection,把它所有 ``tags`` 小写化,做 substring 命中
        user_message(也小写化)。每个 type 选**命中 tag 数最多**的 connection;
        平票时按 ``name`` 字典序选,保证 deterministic。

        无任何 tag 命中的 type 直接不写入返回。

        为什么用 tags
        ------------
        ``connection.tags`` 是 admin 在后台显式设的**集群识别关键词**,是 single
        source of truth。**完全不在代码里写死任何集群别名 / IP / 节点名**——新增集群
        只要在 admin UI 给 connection 打 tag,平台立刻能识别。

        Args:
            user_message: 用户原话(任意长度);空字符串/None 时返回空 dict。

        Examples:
            tags=['bigdata', 'bigdata-swarm', '169.24.2.193'] + user="bigdata-swarm 巡检"
                → 命中 2 个 tag(bigdata + bigdata-swarm),作为该 type 的胜者
        """
        if not user_message:
            return {}
        text = user_message.lower()

        try:
            conns = self.runtime.connection_manager.list()
        except Exception as exc:  # pragma: no cover(防御性)
            logger.warning("自动集群路由:列 connection 失败,跳过:%s", exc)
            return {}

        # 按 type 分组候选 ——【connection, hit_tag_list】
        by_type: dict[str, list[tuple[dict, list[str]]]] = {}
        for c in conns:
            if not c.get("enabled", True):
                continue
            type_code = c.get("type_code")
            if not type_code:
                continue
            tags = [str(t).lower().strip() for t in (c.get("tags") or []) if str(t).strip()]
            hits = [t for t in tags if t and t in text]
            if not hits:
                continue
            by_type.setdefault(type_code, []).append((c, hits))

        selected: dict[str, str] = {}
        for type_code, candidates in by_type.items():
            # 命中 tag 数多者胜;平票按 name 字典序(deterministic 复测)
            candidates.sort(key=lambda x: (-len(x[1]), x[0].get("name", "")))
            winner_conn, winner_hits = candidates[0]
            selected[type_code] = winner_conn["id"]
            logger.info(
                "auto-route [%s]: → %s (alias=%r, hit_tags=%s, candidates=%d)",
                type_code, winner_conn["id"], winner_conn.get("alias"),
                winner_hits, len(candidates),
            )
        return selected

    def _maybe_runbook_preroute(
        self,
        user_message: str,
        ctx: SkillContext,
    ) -> AgentOutcome | None:
        """Runbook 强路由:用户消息命中 trigger 关键词 → 强制执行 runbook。

        返回值语义:
          - ``AgentOutcome`` —— 命中并执行成功,直接当作最终回复,调用方应原样返回
          - ``None``         —— 未命中或不可用,调用方应继续走原 tool loop

        为什么不依赖模型自己识别"复合场景"调 platform_run_runbook
        =========================================================
        实测国产模型对这条 prompt 的遵循度低——同样的"巡检"问题,7b/13b 模型常常
        漏掉 platform_run_runbook,自己拆 N 个 swarm_query 拼,结果跑偏。

        在 agent 循环最前面做一次确定性的 trigger 匹配,可以保证:
          1. 标准化流程一定走 runbook(SOP 保证)
          2. tool loop 步数从 N 步 → 1 步,token 消耗骤降
          3. 任何新增 runbook 都自动享受路由,**无需改 prompt 也无需改 agent**
        """
        registry = getattr(self.runtime, "runbook_registry", None)
        if registry is None:
            return None

        try:
            rb = registry.match_by_query(user_message or "")
        except Exception as exc:  # pragma: no cover  (防御性)
            logger.warning("runbook 预路由匹配抛异常,fallback 到 tool loop:%s", exc)
            return None
        if rb is None:
            return None

        logger.info(
            "runbook 预路由命中 [%s],强制走 platform_run_runbook(绕过模型 tool 决策)",
            rb.key,
        )

        args = {"user_query": user_message, "inputs": {}}
        envelope = self.invoker.invoke("platform_run_runbook", args, ctx)
        trace = [self._to_trace_item("platform_run_runbook", args, envelope)]

        result = envelope.get("result") or {}
        final_report = (result.get("final_report") or "").strip()
        if not final_report:
            # runbook 跑完但没生成最终报告(报告模型挂掉 / global_status != done):
            # 给个简要兜底,让用户知道发生了什么,可以去 admin UI 看完整 trace。
            final_report = (
                f"⚠️ 已经按剧本 ``{rb.key}`` 自动执行,但模型未生成最终报告。\n"
                f"\n"
                f"- global_status: ``{result.get('global_status')}``\n"
                f"- abort_reason: ``{result.get('abort_reason')}``\n"
                f"- total_ms: {result.get('total_ms')}\n"
                f"\n"
                f"原始 ``node_states`` 已落审计,可在管理后台 → Runbook 执行历史查看。"
            )

        return AgentOutcome(
            message=final_report,
            trace=trace,
            pending_actions=[],
        )

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
