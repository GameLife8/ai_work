"""Skill 域定义 + 渐进披露（progressive disclosure）的工具加载。

设计动机
========
以前 27 个 skill 的 schema（~22K 字符）**每轮全发给模型**（或按关键词意图静态
过滤）。两个毛病：

1. 关键词意图过滤**脆**且会**挡掉 signal 想要的 skill**——scanner 发
   ``next_skill=zabbix_get_host_overview``，但该 skill 被意图过滤掉了，模型调不动。
2. 工具集随 query 变 → **破坏 prompt cache**。

新架构：**核心常驻 + 按域懒加载**
================================
- **Layer 0（常驻，~7K）**：``CORE_SKILLS`` —— 三把通用查询口 + zabbix 概览 +
  runbook 入口 + ``load_skills`` meta-tool。覆盖 ~80% 的问题，且固定 → 完美缓存。
- **Layer 1（按需）**：模型发现需要专科能力（写操作 / 监控细节 / CI 等）时，
  调 ``load_skills(domains=[...])``，平台把那个域的 skill schema 加进工具集，
  下一轮即可调用。
- **signal 驱动自动加载**：scanner 发的 ``next_skill`` 若在某个域里，平台**自动**
  把该域加载进来——模型立刻能遵循 signal，不用先 load（解决"挡 signal"硬伤）。

为什么按域而不是按 skill 名
==========================
模型不用记 27 个 skill 全名，只需判断"我现在要干哪类事"（写 swarm / 看监控 / 跑 CI），
6-8 个域好枚举、容错高。
"""

from __future__ import annotations

from typing import Any


# ---- Layer 0：常驻核心（永远加载，覆盖 ~80%）---- #
CORE_SKILLS: frozenset[str] = frozenset({
    "kube_query",                  # k8s 只读一把口（含 verb=logs）
    "swarm_query",                 # swarm 只读一把口
    "host_query",                  # 主机只读一把口（白名单命令）
    "zabbix_get_host_overview",    # 主机监控概览（高频）
    "platform_run_runbook",        # 复合问题编排入口
    "platform_get_runbooks",       # runbook 发现
})


# ---- Layer 1：按域懒加载 ---- #
# domain → 该域的 skill code 列表。模型调 load_skills(domains=[...]) 加载。
SKILL_DOMAINS: dict[str, list[str]] = {
    "swarm_write": [
        "swarm_scale_service", "swarm_update_service_image",
        "swarm_force_update_service", "swarm_rollback_service",
        "swarm_remove_service",
    ],
    "k8s_write": [
        "k8s_scale_deployment", "k8s_restart_deployment", "k8s_rollout_undo",
    ],
    "host_exec": [
        # 主机命令执行（白名单覆盖不到的）+ 异步任务 + 抓包
        "host_run_command", "host_run_command_async", "host_capture_packets",
        "host_check_task", "host_list_tasks", "host_list_nodes",
    ],
    "monitoring": [
        # zabbix 概览已在 core；这里是监控的"细节"层
        "zabbix_get_host_storage_overview",   # 磁盘各挂载点
        "metric_query",                       # 时序（峰值 / 时刻附近）
        "swarm_cluster_overview",             # 集群级聚合巡检
    ],
    "network_diag": [
        "host_inspect_container_netns", "host_kernel_events",
    ],
    "cicd": [
        "jenkins_query",
    ],
    "alerts": [
        "alerts_analyze_payload",
    ],
}


# domain 的人类可读说明——拼进 load_skills 的描述让模型知道每个域装什么。
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "swarm_write": "Swarm 写操作（扩缩容 / 换镜像 / 强制更新 / 回滚 / 删服务）",
    "k8s_write": "K8s 写操作（扩缩 deployment / 重启 / 回滚）",
    "host_exec": "主机命令执行（host_run_command 短/长 + 抓包 + 异步任务轮询）——"
                 "host_query 白名单覆盖不到的命令（docker / virsh 等）走这里",
    "monitoring": "监控细节（磁盘各挂载点 / 指标时序峰值 / 集群级巡检聚合）",
    "network_diag": "网络/内核深度排查（进容器 netns 看 iptables / dmesg 内核事件）",
    "cicd": "Jenkins CI/CD 查询（job / build / console / 队列 / 节点）",
    "alerts": "告警 payload 结构化预分析",
}


# ---- meta-tool schema ---- #
def load_skills_tool_schema() -> dict[str, Any]:
    """``load_skills`` meta-tool 的 OpenAI tool schema。

    放进 Layer 0，让模型按需把某个域的真实 skill 加载进工具集。
    """
    domain_lines = "\n".join(
        f"  - ``{d}``：{DOMAIN_DESCRIPTIONS[d]}" for d in SKILL_DOMAINS
    )
    return {
        "type": "function",
        "function": {
            "name": "load_skills",
            "description": (
                "**按需加载某个能力域的工具**。你默认只能看到核心查询口"
                "（kube_query / swarm_query / host_query / zabbix_get_host_overview）"
                "+ runbook 入口。需要**写操作 / 监控细节 / 主机命令执行 / CI / 告警分析**等"
                "专科能力时，先调本工具把对应域加载进来，**下一轮**就能调用该域的真实 skill。\n"
                "可用域：\n" + domain_lines + "\n"
                "可一次加载多个域：``load_skills(domains=[\"swarm_write\",\"monitoring\"])``。"
                "已加载的域再调无害（幂等）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(SKILL_DOMAINS)},
                        "description": "要加载的能力域，可多个。",
                    },
                },
                "required": ["domains"],
            },
        },
    }


LOAD_SKILLS_NAME = "load_skills"


# ---- 反查：skill → domain（signal 驱动自动加载用）---- #
_SKILL_TO_DOMAIN: dict[str, str] = {
    code: domain
    for domain, codes in SKILL_DOMAINS.items()
    for code in codes
}


def domain_for_skill(skill_code: str) -> str | None:
    """skill 属于哪个域；core skill 或未知返回 None。"""
    return _SKILL_TO_DOMAIN.get(skill_code)


def skills_in_domains(domains: list[str]) -> list[str]:
    """把域列表展开成 skill code 列表（去重、忽略未知域）。"""
    out: list[str] = []
    seen: set[str] = set()
    for d in domains or []:
        for code in SKILL_DOMAINS.get(d, []):
            if code not in seen:
                seen.add(code)
                out.append(code)
    return out


def build_core_tools(full_tools: list[dict], *, include_meta: bool = True) -> list[dict]:
    """从全集 tool schema 里挑出 Layer 0 常驻集 + load_skills meta-tool。

    Args:
        full_tools: ``registry.openai_tools()`` 的全集。
        include_meta: 是否带上 load_skills meta-tool（测试可关）。
    """
    core = [t for t in full_tools
            if t.get("function", {}).get("name") in CORE_SKILLS]
    if include_meta:
        core.append(load_skills_tool_schema())
    return core


def expand_tools(
    full_tools: list[dict],
    current_tools: list[dict],
    new_skill_codes: list[str],
) -> tuple[list[dict], list[str]]:
    """把 new_skill_codes 对应的 tool schema 追加进 current_tools（去重）。

    Returns:
        (新的 tools 列表, 实际新增的 skill code 列表)
    """
    have = {t.get("function", {}).get("name") for t in current_tools}
    by_name = {t.get("function", {}).get("name"): t for t in full_tools}
    added: list[str] = []
    out = list(current_tools)
    for code in new_skill_codes:
        if code in have:
            continue
        tool = by_name.get(code)
        if tool is None:
            continue   # admin-only skill 对 user 不可见 / 未注册
        out.append(tool)
        have.add(code)
        added.append(code)
    return out, added
