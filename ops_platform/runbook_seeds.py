"""平台首启时把这些剧本 seed 到 ``platform_runbook`` 表。

每条剧本是图形式（DAG），可被 RunbookExecutor 自动执行。这是把"运维老司机"的查问题
套路代码化的最终形态。

新增剧本只要往 ``DEFAULT_RUNBOOKS`` 里加一项；改动现有剧本时，admin 在后台编辑覆盖
即可，下次 restart 不会被 seed 覆盖（只在表为空时 seed）。

重构注 (skill 体系简化)
------------------------
原 runbook 引用的浅封装 skill 已被通用查询 skill 替代：
- ``swarm_check_service_health`` / ``swarm_get_failed_tasks`` / ``swarm_get_service_detail``
  / ``swarm_get_service_logs_filter``    →  ``swarm_query`` 配 category/verb
- ``k8s_list_pods`` / ``k8s_describe_pod`` / ``k8s_get_pod_logs``  →  ``kube_query``
- ``host_socket_overview`` / ``host_iptables_dump`` / ``host_route_overview``
  / ``host_kernel_events``                                         →  ``host_query``
"""

from __future__ import annotations

import logging
from typing import Any

from ops_platform.runbook_engine import (
    RunbookLoadError,
    load_runbook_from_dict,
    validate_runbook,
)


logger = logging.getLogger(__name__)


# ============================================================================
# 默认剧本（图形式）
# ============================================================================

DEFAULT_RUNBOOKS: list[dict[str, Any]] = [

    # ------------------------------------------------------------------
    # Swarm 服务起不来 / 持续重启
    # ------------------------------------------------------------------
    {
        "key": "swarm_service_not_starting",
        "title": "Swarm 服务起不来 / 持续重启",
        "description": "覆盖大多数 Swarm 服务异常根因：OOM / 磁盘满 / 镜像拉不下来 / 配置错。",
        "triggers": ["起不来", "启动失败", "不停重启", "CrashLoop", "swarm 异常", "服务异常"],
        "inputs": ["service_name"],
        "start_node": "status",
        "max_total_seconds": 240,
        "nodes": {
            "status": {
                "skill": "swarm_query",
                "description": "服务整体状态（副本/更新状态）",
                "args": {"category": "service", "verb": "inspect",
                         "name": "$user.service_name"},
                "edges": [{"target": "failed_tasks"}],
            },
            "failed_tasks": {
                "skill": "swarm_query",
                "description": "近期失败任务的 exit code + Node",
                "args": {"category": "service", "verb": "ps",
                         "name": "$user.service_name",
                         "filters": {"desired-state": "failed"}},
                "edges": [
                    {"target": "host_overview", "label": "if oom_kill",
                     "when": {"type": "has_signal", "signal_type": "oom_kill"}},
                    {"target": "host_storage", "label": "if no_space",
                     "when": {"type": "has_signal", "signal_type": "no_space_left"}},
                    {"target": "service_detail", "label": "if image_pull_fail",
                     "when": {"type": "has_signal", "signal_type": "image_pull_fail"}},
                    {"target": "logs_error"},
                ],
            },
            "host_overview": {
                "skill": "zabbix_get_host_overview",
                "description": "OOM 触发：看宿主机 CPU/内存压力",
                "args": {"host_query": "$signals.oom_kill.context.node"},
                "on_error": "skip",
                "edges": [{"target": "logs_error"}],
            },
            "host_storage": {
                "skill": "zabbix_get_host_storage_overview",
                "description": "磁盘信号：看具体哪个挂载点满",
                "args": {"host_query": "$signals.no_space_left.context.node"},
                "on_error": "skip",
                "edges": [{"target": "logs_error"}],
            },
            "service_detail": {
                "skill": "swarm_query",
                "description": "镜像拉取失败：看完整镜像引用 + 凭证配置",
                "args": {"category": "service", "verb": "inspect",
                         "name": "$user.service_name"},
                "on_error": "skip",
            },
            "logs_error": {
                "skill": "swarm_query",
                "description": "应用层 error 日志（关键字过滤）",
                "args": {"category": "service", "verb": "logs",
                         "name": "$user.service_name",
                         "filters": {"grep": "error", "tail": 500}},
                "on_error": "skip",
                "edges": [{"target": "logs_oom",
                           "when": {"type": "not", "conditions": [
                               {"type": "has_signal", "signal_type": "oom_kill"},
                           ]}}],
            },
            "logs_oom": {
                "skill": "swarm_query",
                "description": "再换 OOM 关键词捞一次（应用层 java OOM 等）",
                "args": {"category": "service", "verb": "logs",
                         "name": "$user.service_name",
                         "filters": {"grep": "OOM", "tail": 500}},
                "on_error": "skip",
            },
        },
    },

    # ------------------------------------------------------------------
    # K8s Pod CrashLoopBackOff
    # ------------------------------------------------------------------
    {
        "key": "k8s_pod_crashloop",
        "title": "K8s Pod CrashLoopBackOff / 不断重启",
        "description": "K8s 视角的 pod 异常排查；先 list 找异常，再 describe 看 Events，再拉日志。",
        "triggers": ["CrashLoopBackOff", "Pod 重启", "OOMKilled", "k8s pod 异常", "Evicted"],
        "inputs": [],
        "optional_inputs": ["namespace", "pod_name", "label_selector"],
        "start_node": "list",
        "nodes": {
            "list": {
                "skill": "kube_query",
                "description": "namespace 下整体状态、谁有问题（output=json 让 scanner 自动扫 waiting_reasons）",
                "args": {"verb": "get", "resource": "pods",
                         "namespace": "$user.namespace",
                         "output": "json",
                         "selector": "$user.label_selector"},
                "edges": [{"target": "describe"}],
            },
            "describe": {
                "skill": "kube_query",
                "description": "看 Events 找根因",
                # Fallback 链：用户传 → list 里 scanner 抓到的 crashloop/imagepull pod →
                # parsed.items[0]。覆盖 "用户没传 pod_name 整链全 skip" 的死路。
                "args": {
                    "verb": "describe",
                    "resource": "pod",
                    "name": (
                        "$user.pod_name"
                        "||$signals.crash_loop_backoff.next_args.name"
                        "||$signals.image_pull_fail.next_args.name"
                        "||$nodes.list.parsed.items[0].metadata.name"
                    ),
                    "namespace": (
                        "$user.namespace"
                        "||$signals.crash_loop_backoff.next_args.namespace"
                        "||$nodes.list.parsed.items[0].metadata.namespace"
                    ),
                },
                "on_error": "continue",
                "edges": [
                    {"target": "host_check_oom", "label": "if oom_kill",
                     "when": {"type": "has_signal", "signal_type": "oom_kill"}},
                    {"target": "host_check_disk", "label": "if disk evicted",
                     "when": {"type": "all_of", "conditions": [
                         {"type": "has_signal", "signal_type": "pod_evicted"},
                     ]}},
                    {"target": "logs_previous", "label": "if crashloop",
                     "when": {"type": "has_signal", "signal_type": "crash_loop_backoff"}},
                    {"target": "logs_now"},
                ],
            },
            "host_check_oom": {
                "skill": "zabbix_get_host_overview",
                "description": "OOM：从 describe 提取 Node 反查宿主机",
                "args": {"host_query": "$signals.oom_kill.context.node"},
                "on_error": "skip",
            },
            "host_check_disk": {
                "skill": "zabbix_get_host_storage_overview",
                "description": "Evicted+DiskPressure：查节点磁盘",
                "args": {"host_query": "$signals.pod_evicted.context.node"},
                "on_error": "skip",
            },
            "logs_previous": {
                "skill": "kube_query",
                "description": "CrashLoop 必须 previous=true 看上次崩溃前的输出",
                "args": {
                    "verb": "logs",
                    "name": (
                        "$user.pod_name"
                        "||$signals.crash_loop_backoff.next_args.name"
                        "||$nodes.list.parsed.items[0].metadata.name"
                    ),
                    "namespace": (
                        "$user.namespace"
                        "||$nodes.list.parsed.items[0].metadata.namespace"
                    ),
                    "previous": True,
                },
                "on_error": "skip",
            },
            "logs_now": {
                "skill": "kube_query",
                "description": "当前日志兜底",
                "args": {
                    "verb": "logs",
                    "name": (
                        "$user.pod_name"
                        "||$signals.crash_loop_backoff.next_args.name"
                        "||$nodes.list.parsed.items[0].metadata.name"
                    ),
                    "namespace": (
                        "$user.namespace"
                        "||$nodes.list.parsed.items[0].metadata.namespace"
                    ),
                },
                "on_error": "skip",
            },
        },
    },

    # ------------------------------------------------------------------
    # 网络不通排障（用 host_agent + nsenter）
    # ------------------------------------------------------------------
    {
        "key": "network_troubleshooting",
        "title": "网络不通 / 端口连不上 / 丢包",
        "description": "通过 host_agent + nsenter 在宿主机视角排查网络问题，不依赖 SSH。",
        "triggers": ["连不上", "ping 不通", "telnet 不通", "端口不通", "网络抖动", "丢包", "DNAT", "iptables"],
        "inputs": ["node"],
        "optional_inputs": ["port_filter"],
        "start_node": "list_nodes",
        "nodes": {
            "list_nodes": {
                "skill": "host_list_nodes",
                "description": "确认目标 node 上有 agent",
                "args": {},
                "edges": [{"target": "sockets"}],
            },
            "sockets": {
                "skill": "host_query",
                "description": "宿主机视角看监听端口",
                "args": {"node": "$user.node", "command": "ss -ltnup"},
                "edges": [{"target": "iptables"}],
            },
            "iptables": {
                "skill": "host_query",
                "description": "看防火墙是否拦了端口",
                "args": {"node": "$user.node", "command": "iptables-save",
                         "probe_port": "$user.port_filter"},
                "on_error": "skip",
                "edges": [{"target": "routes"}],
            },
            "routes": {
                "skill": "host_query",
                "description": "路由表",
                "args": {"node": "$user.node", "command": "ip route"},
                "on_error": "skip",
                "edges": [{"target": "kernel"}],
            },
            "kernel": {
                "skill": "host_kernel_events",
                "description": "内核事件（看 conntrack table full / drop / NIC 错；自动兼容老节点 dmesg）",
                "args": {"node": "$user.node", "keyword": "conntrack"},
                "on_error": "skip",
            },
        },
    },

    # ------------------------------------------------------------------
    # 主机告警深度健康检查
    # ------------------------------------------------------------------
    {
        "key": "host_resource_alert",
        "title": "主机告警深度健康检查",
        "description": "Zabbix 概览 + 磁盘 + 内核事件三件套，覆盖宿主机绝大部分异常。",
        "triggers": ["主机CPU高", "内存高", "磁盘满", "宕机", "Zabbix 告警", "节点抖动", "Node NotReady"],
        "inputs": ["host_query"],
        "optional_inputs": ["agent_node"],
        "start_node": "overview",
        "nodes": {
            "overview": {
                "skill": "zabbix_get_host_overview",
                "args": {"host_query": "$user.host_query"},
                "edges": [{"target": "storage"}],
            },
            "storage": {
                "skill": "zabbix_get_host_storage_overview",
                "args": {"host_query": "$user.host_query"},
                "on_error": "skip",
                "edges": [{
                    "target": "kernel",
                    "label": "if agent_node provided",
                    "when": {"type": "field_ne", "path": "$user.agent_node", "value": None},
                }],
            },
            "kernel": {
                "skill": "host_kernel_events",
                "description": "Zabbix 看不到的内核层事件（OOM / I/O error / hardware error；自动兼容老节点）",
                "args": {"node": "$user.agent_node", "keyword": "oom"},
                "if_when": {"type": "field_ne", "path": "$user.agent_node", "value": None},
                "on_error": "skip",
            },
        },
    },
]


def seed_default_runbooks(store) -> dict[str, list[str]]:
    """如果 ``platform_runbook`` 表是空的，把 DEFAULT_RUNBOOKS 写入并返回每条的 validate 结果。

    幂等：表非空时不动；admin 改过的 runbook 不会被覆盖。
    """
    existing = []
    try:
        existing = store.list_runbooks()
    except Exception as exc:  # pragma: no cover
        logger.warning("seed: list_runbooks 失败：%s", exc)
        return {}

    if existing:
        logger.info("seed: 已有 %d 条 runbook，跳过 seed", len(existing))
        return {}

    seeded: dict[str, list[str]] = {}
    for definition in DEFAULT_RUNBOOKS:
        try:
            rb = load_runbook_from_dict(definition)
        except RunbookLoadError as e:
            seeded[definition.get("key", "?")] = [f"load 失败：{e}"]
            continue
        errs = validate_runbook(rb)
        if errs:
            seeded[rb.key] = errs
            logger.warning("seed runbook %s 校验有问题（仍写入，admin 后台修）：%s", rb.key, errs)
        store.upsert_runbook(
            key=rb.key,
            title=rb.title,
            description=rb.description,
            triggers=rb.triggers,
            inputs=rb.inputs,
            definition=definition,
            enabled=True,
            updated_by="bootstrap",
        )
        seeded.setdefault(rb.key, [])
    logger.info("seed: 写入 %d 条默认 runbook", len(seeded))
    return seeded
