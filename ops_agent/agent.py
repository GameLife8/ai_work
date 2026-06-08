from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ops_agent.skill_domains import (
    LOAD_SKILLS_NAME,
    build_core_tools,
    domain_for_skill,
    expand_tools,
    skills_in_domains,
)
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
#   2. **分类错不再影响工具能力**——工具加载已换成渐进披露（skill_domains.py），
#      意图分类现在只影响"输出格式 hint + 写操作 tool_choice"这种软提示，分错最多
#      格式略偏，不会让模型缺工具。
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
    # E 类：写操作
    "write_action": [
        "重启服务", "扩容", "缩容", "扩到", "缩到", "扩成", "改成",
        "回滚", "删除服务", "下线服务", "强制更新", "改镜像",
        "替换镜像", "重新部署", "升级版本",
        # 高频"执行某条具体命令"变体——之前漏掉导致用户说"docker prune -f 请用这条
        # 命令清理一下"时模型不知道这是写意图,只输出描述不调 tool:
        "请使用", "请执行", "请运行", "请帮我执行", "请帮我运行",
        "使用这条", "执行这条", "运行这条", "跑这条", "用这条",
        "帮我清理", "清理一下", "清一下", "执行下", "运行下", "跑下",
        "立刻执行", "马上执行", "现在执行",
        # 写操作的中性动词(单独不一定是写意图,组合后更高频)
        "kill", "rm -rf", "prune", "drop ",
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


# NOTE:_INTENT_TOOLS + _filter_tools_by_intent（关键词→静态白名单过滤）已删除，
# 换成 ops_agent/skill_domains.py 的渐进披露（核心常驻 + load_skills 按域懒加载）。
# 原因:静态白名单会挡掉 signal 想要的 skill，且工具集随 query 变破坏 prompt cache。
# _classify_intent 保留——仅给输出格式 hint + 写操作 tool_choice 用，不再碰工具加载。


_INTENT_HINTS: dict[str, str] = {
    "config_view": (
        "🎯 **本次意图:查看配置 / 看原文(B 类)**——执行铁律 #2「原始数据展示优先」。\n"
        "1. **先**用 ``` 代码块原样贴配置原文(yaml/json,取 result.parsed 或 stdout),"
        "完整不重排不省字段;超长(>200 行)按段贴关键段"
        "(image/replicas/networks/labels/placement/healthcheck/env/mounts)。\n"
        "2. **再**用 1-3 段中文逐项翻译关键字段。\n"
        "3. 可选一句运维建议。"
    ),
    "list_state": (
        "🎯 **本次意图:列表 / 状态总览(C 类)**——执行铁律 #2「原始数据展示优先」。\n"
        "1. **第一段先贴原始列表**:命令输出(``docker ps``/``ss -ltnp``)→ ``` 代码块;"
        "JSON/结构化(``kubectl get -o json`` items)→ markdown 表格,挑关键列"
        "(名字/状态/镜像/副本/重启次数/创建时间/Node/端口)。\n"
        "2. **第二段**(1-3 句)解读:多少个、几个异常、哪些值得关注;异常项行首加 ⚠️/🚨。\n"
        "3. 有异常时主动追问:「想看哪个的 describe / 日志?」"
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
        "🎯 **本次用户意图:写操作(E 类)** —— 完整规则见 system prompt 顶部 "
        "「## 写操作」段。要点:**首轮调 tool**(平台已开 tool_choice=required 强制),"
        "第二轮基于 needs_confirmation 写 2-3 句:原因 → 风险/回滚 → "
        "结尾「请在下方点击 ✅ 确认 或 ❌ 取消」。**不要重写卡片已有的 skill/参数/有效期**。"
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
    priority = ["write_action", "monitor",
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


# ====================================================================
# raw-first 展示 —— **产品契约**（不是模型兜底）
# ====================================================================
# 背景:system prompt 铁律 #2 已极其明确要求"看配置/列表场景先贴原文再解读",
# 但实测国产模型(doubao-seed-2-0-pro)**拿着 3496 字符的 Corefile 就是不贴**,
# 写 5 段散文概括。纯 prompt 在该模型上不可靠。
#
# 设计取舍:
#   - 这是**产品契约**——用户问"展示 X 配置"就必须看到 X 原文,跟模型听不听话无关。
#   - 跟"删兜底"不矛盾:删的是"平台替模型决策"(关键词路由 / 合成 tool_call);
#     这个是"平台保证 UX 一致性"。两码事。
#   - **保留遥测**:每次契约触发都 logger.warning,记录模型未遵循铁律#2 的情况
#     → 既保证 UX,又能量化模型遵循率(回应"删兜底是为了看模型听不听话"的诉求)。
#
# 作用域:只在 config_view / list_state 意图(用户明确"要看数据")触发;诊断/写操作
# 场景不碰(那里散文/提议才是对的输出)。

# raw-first 契约**不门控在特定意图**(避免关键词分类没命中就失效的脆弱)。
# 改成"排除法":只跳过这两类"散文输出才对"的场景——
#   - write_action:模型给的是"我打算执行X 请确认"提议,不该塞 raw
#   - diagnose:五段式诊断报告是刻意格式,证据已在"关键证据"段的 ``` 里
# 其余所有意图(config_view / list_state / monitor / knowledge / **unknown**)
# 只要 trace 里有可展示数据且模型没贴 raw,都补。
# 关键:**unknown(None) 也覆盖**——"看其他配置"哪怕关键词没命中 config_view、
# 落到 unknown,照样补 raw。这就是回应"换个说法看配置兜底还生效么"的设计。
_RAW_FIRST_SKIP_INTENTS = frozenset({"write_action", "diagnose"})

# 输出"可枚举/可展示"原文的只读 skill
_DISPLAYABLE_READ_SKILLS = frozenset({
    "kube_query", "swarm_query", "host_query",
    "swarm_cluster_overview", "k8s_cluster_overview",
    "zabbix_get_host_overview", "zabbix_get_host_storage_overview", "jenkins_query",
})


def _message_has_raw(message: str) -> bool:
    """模型自己已经贴了 raw(``` 代码块 或 markdown 表格分隔行)?"""
    if not message:
        return False
    if "```" in message:
        return True
    return bool(re.search(r"^\s*\|[\s\-:|]+\|\s*$", message, re.M))


def _extract_raw_for_display(item: dict[str, Any]) -> tuple[str, str]:
    """从 trace item 抽可直接贴的 raw 文本,返回 (text, lang)。空则 ("","")。

    优先级:compose_yaml(用户最熟) > stdout(命令/kubectl 原文) > parsed(转 JSON)。
    """
    result = item.get("tool_result")
    if not isinstance(result, dict):
        return "", ""
    cy = result.get("compose_yaml")
    if isinstance(cy, str) and cy.strip():
        return cy.strip(), "yaml"
    stdout = result.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        return stdout.strip(), ""
    parsed = result.get("parsed")
    if parsed not in (None, "", [], {}):
        try:
            return json.dumps(parsed, ensure_ascii=False, indent=2), "json"
        except (TypeError, ValueError):
            pass
    return "", ""


def _ensure_raw_displayed(
    message: str,
    trace: list[dict[str, Any]],
    intent: str | None,
    *,
    model_name: str = "",
    max_chars: int = 8000,
) -> str:
    """产品契约:模型给纯散文(没贴 raw)但 trace 里有可展示数据时,平台补贴。

    **不依赖意图分类命中**——只排除 write_action / diagnose(散文输出才对)。
    "查看任何配置/数据"都生效,包括关键词没命中、落到 unknown 的说法。

    触发条件(全满足):
      1. intent ∉ _RAW_FIRST_SKIP_INTENTS(即非 write_action/diagnose)
      2. message 非空且**还没**贴 raw(无 ``` 无表格)
      3. trace 里有成功的 displayable read-query,且能抽出 raw + 无 pending_token
    """
    if intent in _RAW_FIRST_SKIP_INTENTS:
        return message
    if not message or _message_has_raw(message):
        return message
    for item in reversed(trace):
        if item.get("status") != "ok":
            continue
        if item.get("tool_name") not in _DISPLAYABLE_READ_SKILLS:
            continue
        if item.get("pending_token"):          # 写操作待确认场景不补
            continue
        raw, lang = _extract_raw_for_display(item)
        if not raw:
            continue
        shown = raw if len(raw) <= max_chars else (
            raw[:max_chars] + "\n... (原文过长已截断，完整见调用 trace)"
        )
        # 遥测:模型在"要看数据"意图下没贴 raw,平台补贴。用来量化模型遵循率。
        logger.warning(
            "raw-first 契约触发:model=%s intent=%s 未贴 raw,平台补 skill=%s(%d 字符)",
            model_name or "?", intent, item.get("tool_name"), len(raw),
        )
        return (
            message
            + f"\n\n---\n📋 **原始数据**（平台自动展示）：\n```{lang}\n{shown}\n```"
        )
    return message


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

    def _build_cluster_registry_prompt(
        self, *, selected_connections: dict[str, str], user_message: str = "",
    ) -> str:
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

        # #3:把"用户本次大概率指的集群"钉死——预路由(歧义/不兼容)放弃后,模型不再飘到默认集群。
        resolved = self._resolve_clusters_from_query(user_message) if user_message else {}
        resolved_picks: list[str] = []
        for _t, _cid in resolved.items():
            _conn = next((c for c in conns if c.get("id") == _cid), None)
            if _conn:
                resolved_picks.append(
                    f"``{_conn.get('alias') or _conn.get('name')}``（{_t}, connection_id={_cid}）"
                )

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
        if resolved_picks:
            lines.insert(2, (
                "🎯 **你本次大概率指的集群**：" + " · ".join(resolved_picks)
                + "。涉及这些 type 的 skill / runbook **必须传对应 connection_id**，"
                "除非用户明确改指别的集群。"
            ))
            lines.insert(3, "")

        # 优先级：常用类型靠前
        type_order = ["host_agent", "swarm", "k8s", "zabbix", "jenkins", "alert_analysis"]
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
                if resolved.get(t) == cid:
                    flag_tags.append("[🎯本次命中]")
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
            "节点命名规律 → 调 ``host_run_command(node='bigdata6', command='df -h')``(平台按 node 自动路由集群)。",
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
        if type_code == "jenkins":
            return f"Jenkins = ``{cfg.get('base_url','')}``"
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
        #   (走 tool loop 时让模型自己挑 connection_id);自动路由覆盖"模型不参与
        #   决策"的路径(主要是 runbook 预路由,直接强制走 platform_run_runbook)。
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

        # ---- 意图分类（仅用于输出格式 hint + 写操作 tool_choice，不再做工具过滤）----
        intent = _classify_intent(user_message)

        # ---- 渐进披露：Layer 0 常驻核心 + load_skills meta-tool ----
        # 工具加载从"关键词意图静态过滤"换成"核心常驻 + 按域懒加载"：
        #   - tools 初始 = 三把口 + zabbix 概览 + runbook 入口 + load_skills（~7K，固定→缓存友好）
        #   - 模型调 load_skills(domains=[...]) → 平台把该域 skill 加进 tools，下轮可调
        #   - signal 的 next_skill 在某域里 → 自动加载该域（解决"挡 signal"硬伤）
        # expand_tools 按 skill 名去重，所以重复 load 同域无害（幂等），不必额外记账。
        tools = build_core_tools(full_tools)
        logger.debug("渐进披露：core tools=%d（全集 %d）", len(tools), len(full_tools))

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
                user_message=user_message,
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
        # Token usage 累计：每次 create_completion 后从 message["_usage"] 累加；
        # ask() 结束时塞到 AgentOutcome.usage 给入口层（chainlit_app / API）持久化。
        total_usage: dict[str, Any] = {}

        # ---- tool_choice 策略：写操作意图首轮强制调 tool ----
        # 国产模型(豆包 seed-2 / qwen3 等)对 "tool_choice=auto" 下的写操作意图常常
        # 产生"我打算执行 X..."的纯文本描述,**没有真正调 tool**。强制 ``required`` 让模型
        # 必须先选一个 tool——渐进披露下首轮核心集里写 skill 还没加载,模型会先调
        # ``load_skills(domains=["swarm_write"...])`` 或先用核心查询口确认状态,都正确。
        # 仅首轮(step==0)强制;后续轮次还原 ``auto``,让模型基于 tool 结果自主决策。
        first_round_tool_choice = "required" if intent == "write_action" else "auto"

        for step in range(self.max_steps):
            # ⏬ 调用 LLM 前先压缩 messages，防止 8 步循环里上下文越积越多撑爆窗口
            messages = _maybe_compress(messages)

            tc = first_round_tool_choice if step == 0 else "auto"
            message = self.model.create_completion(
                messages=messages, tools=tools, tool_choice=tc,
            )
            _accumulate_usage(total_usage, message.pop("_usage", {}))
            tool_calls = message.get("tool_calls") or []
            content = (message.get("content") or "").strip()

            if not tool_calls:
                # 模型不调 tool —— 三种情形,直接走结束分支,**不再做兜底**(关键词
                # 启发式 / 合成 tool_call / 强制 retry full set 全删了):
                #   A. 已有 trace → 让模型基于已取证写最终报告
                #   B. 模型直接给了文字答案(content 非空)→ 返回原话
                #   C. 模型既没调也没说 → 返回"未获得明确结论"提示用户重问
                # 这三种都靠 prompt + tool_choice 引导,不再用平台代码救场。
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
                        message=_ensure_raw_displayed(
                            summary.get("content") or "已完成排查，但模型没有输出总结。",
                            trace, intent, model_name=getattr(self.model, "model", "")),
                        trace=trace,
                        pending_actions=pending_actions,
                        usage=total_usage,
                    )
                return AgentOutcome(
                    message=_ensure_raw_displayed(
                        message.get("content") or "未获得明确结论。",
                        trace, intent, model_name=getattr(self.model, "model", "")),
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

                # ---- load_skills meta-tool：不是真 skill，平台直接处理 ----
                # 模型按需把某个域的工具加载进 tools，下一轮即可调用。
                if name == LOAD_SKILLS_NAME:
                    req_domains = args.get("domains") or []
                    codes = skills_in_domains(req_domains)
                    tools, added = expand_tools(full_tools, tools, codes)
                    result_msg = {
                        "loaded_domains": req_domains,
                        "added_skills": added,
                        "hint": ("这些 skill 现在可以调用了" if added
                                 else "请求的域已加载或为空，无新增"),
                    }
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(result_msg, ensure_ascii=False),
                    })
                    logger.debug("load_skills(%s) → 新增 %d 个 skill", req_domains, len(added))
                    continue   # 不进 trace（非业务调用），不走 invoker

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
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": self.invoker.serialize_for_model(envelope),
                })

            # ⚡ Signals 跨域硬 pivot：把"建议下一步"显式喂给模型
            if new_signals and not had_pending:
                # signal 驱动自动加载：scanner 建议的 next_skill 若在某个懒加载域里，
                # 平台**自动**把该域加进 tools——模型下一轮能立刻遵循 signal，
                # 不用先 load_skills 再调（解决静态过滤"挡 signal"硬伤）。
                auto_codes = [
                    s["next_skill"] for s in new_signals
                    if s.get("next_skill") and domain_for_skill(s["next_skill"])
                ]
                if auto_codes:
                    tools, auto_added = expand_tools(full_tools, tools, auto_codes)
                    if auto_added:
                        logger.debug("signal 自动加载：%s", auto_added)
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
                    message=summary.get("content") or "请在下方点击 ✅ 确认 或 ❌ 取消。",
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
            message=_ensure_raw_displayed(
                summary.get("content") or "已完成排查，但模型没有输出总结。",
                trace, intent, model_name=getattr(self.model, "model", "")),
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

    # 只关心"集群类型"——host_agent/zabbix 是辅助,不参与一致性判定
    _CLUSTER_TYPES = frozenset({"swarm", "k8s"})

    def _runbook_required_conn_types(self, rb: Any) -> set[str]:
        """runbook 的各节点 skill 需要哪些 connection type(只取 swarm/k8s 集群类)。"""
        types: set[str] = set()
        nodes = getattr(rb, "nodes", {}) or {}
        for node in nodes.values():
            skill_code = getattr(node, "skill", None)
            if not skill_code:
                continue
            try:
                spec = self.registry.get(skill_code)
            except Exception:
                continue
            rct = getattr(spec, "required_connection_type", None)
            if rct in self._CLUSTER_TYPES:
                types.add(rct)
        return types

    def _pick_cluster_compatible_runbook(
        self, candidates: list[Any], user_message: str,
    ) -> Any | None:
        """从命中候选里挑集群类型兼容用户路由的那个;挑不到返回 None(交模型,**绝不静默跑默认集群**)。

        规则(fail-safe —— "静默巡检错集群"是最坏失败,宁可交模型也不盲猜):
          - 用户**点名**了某集群类型(resolved 有 swarm/k8s)→ 按候选排名挑第一个
            "需要的集群类型 ∈ 用户路由到的类型"(或不绑集群类型)的候选;全不兼容 → None。
          - 用户**没点名** + 候选**跨多个集群类型**(通用"集群巡检"同时命中 swarm/k8s
            两个 audit)→ **歧义,返回 None**——交模型按全量接入清单自己选,不盲选字母序最前的。
          - 用户**没点名** + 候选只涉及**单一**集群类型(或都不绑类型)→ 无歧义,用排名最高的。
        """
        resolved = self._resolve_clusters_from_query(user_message)
        user_cluster_types = set(resolved.keys()) & self._CLUSTER_TYPES

        if user_cluster_types:
            for rb in candidates:
                required = self._runbook_required_conn_types(rb) & self._CLUSTER_TYPES
                if not required or (required & user_cluster_types):
                    return rb
            return None

        # 没点名集群:看候选覆盖几种集群类型
        cand_types: set[str] = set()
        for rb in candidates:
            cand_types |= self._runbook_required_conn_types(rb) & self._CLUSTER_TYPES
        if len(cand_types) > 1:
            # 通用词同时命中 swarm + k8s 巡检 → 歧义,不盲选默认,交模型(带接入清单 + 路由线索)
            return None
        return candidates[0] if candidates else None

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
            candidates = registry.match_all_by_query(user_message or "")
        except Exception as exc:  # pragma: no cover  (防御性)
            logger.warning("runbook 预路由匹配抛异常,fallback 到 tool loop:%s", exc)
            return None
        if not candidates:
            return None

        # ---- 按集群类型挑兼容的 runbook ----
        # 复现 bug:用户"codewave(k8s) 集群巡检",关键词"集群巡检"同时命中 swarm/k8s
        # 两个巡检 runbook。纯关键词只返回一个(可能是 swarm),swarm_cluster_overview
        # 需要 swarm 连接 → codewave 没有 → fallback 默认 swarm,巡检了**错误集群**。
        # 修法:在候选里挑"需要的集群类型 ∈ 用户路由到的集群类型"那个。
        rb = self._pick_cluster_compatible_runbook(candidates, user_message)
        if rb is None:
            logger.info(
                "runbook 预路由跳过:命中的 runbook 集群类型与用户指定的不兼容(候选=%s)",
                [c.key for c in candidates],
            )
            return None

        logger.info(
            "runbook 预路由命中 [%s],强制走 platform_run_runbook(绕过模型 tool 决策)",
            rb.key,
        )

        # 关键:必须把**预路由选中的 rb.key** 传给 platform_run_runbook。
        # 否则它会拿 user_query 再 match 一次(registry.find→match_by_query 取字母序最前),
        # 把这里挑好的"集群类型兼容 runbook"丢掉——这正是 "bigdata(swarm) 却跑了 k8s" 的根因。
        args = {"name": rb.key, "user_query": user_message, "inputs": {}}
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

        # #4 可见性补丁:报告头部固定标明"本次巡检集群",一旦路由跑偏用户一眼可见
        # (这次 bug 正是缺这个,误以为巡检了 bigdata 实际跑了默认 it-cluster01)。
        banner = self._runbook_cluster_banner(rb, user_message)
        if banner and final_report and not final_report.startswith(">"):
            final_report = banner + "\n\n" + final_report

        return AgentOutcome(
            message=final_report,
            trace=trace,
            pending_actions=[],
        )

    def _runbook_cluster_banner(self, rb: Any, user_message: str) -> str:
        """生成"本次巡检集群：<alias>(<type>)"提示行;挑不出集群类型则空串。

        用跟 platform_run_runbook 一致的解析口径:``_resolve_clusters_from_query`` 命中则用
        命中的 connection,否则该 type 的平台默认 connection——保证 banner 跟实际跑的集群一致。
        """
        types = self._runbook_required_conn_types(rb) & self._CLUSTER_TYPES
        if not types:
            return ""
        resolved = self._resolve_clusters_from_query(user_message)
        cm = getattr(self.runtime, "connection_manager", None)
        if cm is None:
            return ""
        parts: list[str] = []
        for t in sorted(types):
            cid = resolved.get(t)
            try:
                conn = cm.get(cid) if cid else cm.get_default(t)
            except Exception:  # noqa: BLE001
                conn = None
            if conn:
                alias = conn.get("alias") or conn.get("name") or "?"
                suffix = "" if cid else "（平台默认）"
                parts.append(f"{alias}（{t}）{suffix}")
        return ("> 🎯 **本次巡检集群**：" + " · ".join(parts)) if parts else ""

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

    # NOTE:``_heuristic_route`` 和 ``_pick_fallback`` 两个兜底方法已删除。
    # 原因:它们用关键词正则替模型决策(磁盘→zabbix_storage / JSON→alerts_analyze
    # 等),叠加多了让"模型到底好不好用"变成黑盒——分不清是真模型行为还是兜底
    # 救了。新策略:出问题就改 prompt + tool_choice,不在平台代码里补救场。
    # 历史代码见 git history (commit b62f008 之前)。
