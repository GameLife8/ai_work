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
- ``host_socket_overview`` / ``host_iptables_dump`` / ``host_kernel_events`` / ``host_query``
  / ``host_inspect_container_netns`` / ``host_capture_packets``    →  全删,统一走唯一执行口
  ``host_run_command``(写 skill,不能进自动 DAG;主机排障改走 runbooks.py 文本剧本)
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
        "description": (
            "覆盖大多数 Swarm 服务异常根因:OOM / 磁盘满 / 镜像拉不下来 / 配置错。"
            "**关键 pivot**:OOM / no_space 信号会触发 swarm node inspect 拿节点 IP 再反查 Zabbix"
            "(因为 Zabbix 注册主机多按 IP,直接用 swarm hostname 查不到)。"
        ),
        "triggers": ["起不来", "启动失败", "不停重启", "CrashLoop", "swarm 异常", "服务异常"],
        "inputs": ["service_name"],
        "start_node": "status",
        "max_total_seconds": 240,
        "nodes": {
            "status": {
                "skill": "swarm_query",
                "description": "服务整体状态(副本/更新状态/spec 完整字段)",
                "args": {"category": "service", "verb": "inspect",
                         "name": "$user.service_name"},
                "edges": [{"target": "failed_tasks"}],
            },
            "failed_tasks": {
                "skill": "swarm_query",
                "description": "近期失败任务 + scanner 自动判 OOM / no_space / image_pull_fail",
                "args": {"category": "service", "verb": "ps",
                         "name": "$user.service_name",
                         "filters": {"desired-state": "failed"}},
                "edges": [
                    # OOM / 磁盘信号 → 先拿 swarm node IP(Zabbix 按 IP 注册,hostname 多半查不到)
                    {"target": "oom_node_lookup", "label": "if oom_kill",
                     "when": {"type": "has_signal", "signal_type": "oom_kill"}},
                    {"target": "disk_node_lookup", "label": "if no_space",
                     "when": {"type": "has_signal", "signal_type": "no_space_left"}},
                    # image_pull_fail 信号本身在 status 节点的 inspect 输出里就能看到完整 image 字段
                    # —— 不需要再来一次重复 inspect。直接走日志兜底,模型从 status 已有的 raw 找镜像名。
                    {"target": "logs_error"},
                ],
            },
            # ⭐ OOM 跨域 pivot:swarm hostname → Status.Addr(节点 IP) → Zabbix
            # 解决"用 swarm Node 名字查 Zabbix 失败"的历史痛点,跟 cluster_health_audit_swarm 同思路
            "oom_node_lookup": {
                "skill": "swarm_query",
                "description": "OOM 触发:把 swarm node hostname 转成 IP",
                "args": {"category": "node", "verb": "inspect",
                         "name": "$signals.oom_kill.context.node"},
                "on_error": "skip",
                "edges": [{"target": "host_overview"}],
            },
            "host_overview": {
                "skill": "zabbix_get_host_overview",
                "description": "用 IP 反查 Zabbix:看节点是否 CPU/内存压力导致 OOM",
                "args": {"host_query": "$nodes.oom_node_lookup.parsed[0].Status.Addr"},
                "on_error": "skip",
                "edges": [{"target": "logs_error"}],
            },
            # ⭐ 磁盘跨域 pivot:同上思路
            "disk_node_lookup": {
                "skill": "swarm_query",
                "description": "磁盘满信号:hostname → IP",
                "args": {"category": "node", "verb": "inspect",
                         "name": "$signals.no_space_left.context.node"},
                "on_error": "skip",
                "edges": [{"target": "host_storage"}],
            },
            "host_storage": {
                "skill": "zabbix_get_host_storage_overview",
                "description": "用 IP 反查 Zabbix:看挂载点用率",
                "args": {"host_query": "$nodes.disk_node_lookup.parsed[0].Status.Addr"},
                "on_error": "skip",
                "edges": [{"target": "logs_error"}],
            },
            "logs_error": {
                "skill": "swarm_query",
                "description": "应用层 error 关键字日志",
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
                "description": "再换 OOM 关键词捞一次(应用层 java OOM 等)",
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
        "description": (
            "K8s 视角的 pod 异常排查:先 list 找异常,scanner 自动 emit crash_loop_backoff / "
            "image_pull_fail / pod_evicted / oom_kill 信号,然后 describe 看 Events,最后拉日志"
            "(CrashLoop 必须 previous=true)。"
            "**已知局限**:跨域查 Zabbix 节点用的是 K8s nodeName,如果 Zabbix 按 IP 注册可能查不到。"
            "K8s nodeName → InternalIP 反查需要 jsonpath ``[*]`` 过滤支持(引擎目前不支持),后续会修。"
        ),
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
                "description": (
                    "当前日志兜底。"
                    "**fallback 链优先取 scanner 已识别的异常 pod**(crashloop / image_pull_fail / pod_evicted),"
                    "最后才退到 ``parsed.items[0]``——避免在 list 全是健康 pod 时拉无诊断价值的正常日志。"
                ),
                "args": {
                    "verb": "logs",
                    "name": (
                        "$user.pod_name"
                        "||$signals.crash_loop_backoff.next_args.name"
                        "||$signals.image_pull_fail.next_args.name"
                        "||$signals.pod_evicted.next_args.name"
                        "||$nodes.list.parsed.items[0].metadata.name"
                    ),
                    "namespace": (
                        "$user.namespace"
                        "||$signals.crash_loop_backoff.next_args.namespace"
                        "||$signals.image_pull_fail.next_args.namespace"
                        "||$signals.pod_evicted.next_args.namespace"
                        "||$nodes.list.parsed.items[0].metadata.namespace"
                    ),
                },
                "on_error": "skip",
            },
        },
    },

    # ------------------------------------------------------------------
    # 主机告警深度健康检查（Zabbix 概览 + 磁盘）
    # ------------------------------------------------------------------
    # 注：原「网络不通排障」「内核事件」DAG 已下线——主机命令统一走 host_run_command
    # (写 skill,每次弹确认),不能进自动执行的 DAG。这类排障改走 runbooks.py 的
    # 文本剧本(network_troubleshooting / container_netns_diag / node_health_audit),
    # 模型读着用 host_run_command 自己跑。
    {
        "key": "host_resource_alert",
        "title": "主机告警深度健康检查",
        "description": "Zabbix 概览 + 磁盘,覆盖宿主机 CPU/内存/可用性/磁盘告警。",
        "triggers": ["主机CPU高", "内存高", "磁盘满", "宕机", "Zabbix 告警", "节点抖动", "Node NotReady"],
        "inputs": ["host_query"],
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
            },
        },
    },

    # ------------------------------------------------------------------
    # Swarm 集群巡检 / 体检 —— 一键拉全
    # ------------------------------------------------------------------
    # 设计要点：把"列节点 → 每节点 inspect 拿 IP → 用 IP 反查 Zabbix → 列服务 → 异常筛选"
    # 这条标准化流程固化进 swarm_cluster_overview 聚合 skill；本剧本只需一个节点
    # 就能完成巡检，模型在末端只写中文表格化报告，token 极省。
    {
        "key": "cluster_health_audit_swarm",
        "title": "Swarm 集群巡检 / 体检",
        "description": (
            "一键拉全：所有节点(含 IP 反查的 Zabbix 监控 CPU/MEM/磁盘) + 所有服务 + 异常清单。"
            "解决「直接用 Swarm node hostname 查 Zabbix 大概率查不到」的痛点——平台主动从 "
            "``docker node inspect`` 提 Status.Addr 是 IP，用 IP 反查。"
        ),
        # 关键词覆盖中英用法、swarm 限定词 + 通用巡检/体检词。
        # 故意把 "巡检 / 体检" 也放进来——但要求 user_query 同时含 swarm 类信号
        # (集群 alias 默认就含 swarm),才会被 match_by_query 选中。
        "triggers": [
            "swarm 巡检", "swarm 体检", "swarm 集群巡检",
            "docker 集群巡检", "docker swarm 巡检",
            "集群巡检", "集群体检", "集群健康检查",
            "整体看一下", "整体看下", "整体情况",
            "集群概况", "集群概览", "cluster audit", "health check",
        ],
        "inputs": [],
        "optional_inputs": ["lookback_hours"],
        "max_total_seconds": 240,
        "start_node": "overview",
        "nodes": {
            "overview": {
                "skill": "swarm_cluster_overview",
                "description": "一把口拉:节点+IP+Zabbix 反查+服务+异常筛选",
                "args": {
                    # 用户可在 inputs 里覆盖,默认 1 小时窗口
                    "lookback_hours": "$user.lookback_hours||1",
                },
                "timeout_seconds": 180,
                "on_error": "fail",
            },
        },
        "final_report_prompt": (
            "你是 AI 运维助手。平台已经把 Swarm 集群 raw 巡检数据采集回来"
            "(读 ``node_states[0].result`` 字段:``swarm`` / ``nodes[]`` / ``services.items[]``)。\n"
            "**异常判定由你做**,平台只采集不判定。\n"
            "\n"
            "# 输出格式硬约束(必须按此结构,顺序/层级/格式一律不许偏离)\n"
            "\n"
            "## 📊 集群概览\n"
            "2-3 句:节点总数 / manager-worker 数 / 服务总数 / Zabbix 监控覆盖率"
            "(found vs total)。\n"
            "\n"
            "## 🖥️ 节点资源\n"
            "\n"
            "**直接输出 markdown 表格(不要用 ``` 代码块包裹,否则 chainlit 渲染成 raw code 而不是表格)**。\n"
            "表头:\n"
            "\n"
            "| Hostname | IP | 角色 | CPU avg | CPU p95 | 内存% | 最高磁盘% | 备注 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "\n"
            "**所有节点都要列(N 节点就 N 行,不许省略)**。每行取值:\n"
            "\n"
            "- **CPU avg / p95**:``nodes[i].zabbix.overview.metric_summary.cpu_avg`` / ``cpu_p95`` (单位 %)\n"
            "- **内存%**:``nodes[i].zabbix.overview.memory_summary.memory_used_percent``\n"
            "- **最高磁盘%**:从 ``nodes[i].zabbix.storage.filesystems[]`` 里挑 ``used_percent`` 最大的那一项,"
            "  写成 ``<mount_point> N%``,例如 ``/var 92%``\n"
            "  * ``filesystems`` 是空列表 → 写 ``-``;``zabbix.storage`` 字段不存在 → 写 ``-``\n"
            "- **备注**:\n"
            "  * 数字 ≥ 80% → ``⚠️ <字段>偏高``\n"
            "  * 数字 ≥ 90% → ``🚨 <字段>危急``(同行有多个就并列写,如 ``🚨 内存危急 / ⚠️ 磁盘偏高``)\n"
            "  * ``zabbix.found == False`` → ``❓ 未纳监控``,该行 CPU/内存/磁盘三列写 ``-``\n"
            "  * 全正常 → ``✅``\n"
            "\n"
            "## ⚙️ 服务运行状态\n"
            "\n"
            "把 ``services.items[].replicas`` 当 ``\"K/N\"`` 字符串解析:\n"
            "- ``\"0/N\"`` → 🚨 全副本宕\n"
            "- ``\"K/N\"`` 且 K<N → ⚠️ 副本不足 K/N\n"
            "- ``\"N/N\"`` → 正常,**不入表**\n"
            "- 解析不了(``\"n/a\"`` 等) → ❓ 状态未知\n"
            "\n"
            "**只列异常服务**(全宕 + 副本不足 + 状态未知)。直接 markdown 表格,**不要 ``` 包裹**:\n"
            "\n"
            "| Service | Mode | Replicas | Image | 问题 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "\n"
            "- 表格下补一句:``其余 N 个服务副本正常``(N = 总服务数 - 异常数)\n"
            "- 如果全部正常:直接写一行 ``✅ 全部 N 个服务副本正常``,**不输出空表格**\n"
            "\n"
            "## 💡 总结 & 建议\n"
            "\n"
            "1-3 条 bullet:本次发现的关键问题(高水位节点 / 异常服务 / 未纳监控节点),"
            "每条配一个具体可执行的下一步(扩容 / 排查 / 加监控)。\n"
            "\n"
            "# 铁律(违反等于错)\n"
            "\n"
            "1. **表格 = 裸 markdown 表格**——前后**不许加 ``` 包裹**,否则 chainlit 渲染成 raw code\n"
            "2. **节点表必须全列**——N 节点 N 行,不许只列前 5 个\n"
            "3. **字段路径要对**:磁盘必须走 ``zabbix.storage.filesystems`` 取 ``mount_point`` / "
            "``used_percent``;字段不存在就写 ``-``,不许凭空补「无数据」字样\n"
            "4. **该警告就警告**——CPU 85% 不许写正常,90% 内存不许写「无明显异常」\n"
            "5. **不许做『无害化』概括**——「整体健康度良好」「未发现明显问题」这种没数据支撑的话一律不许出现\n"
            "6. **数字保留 2 位小数**,例如 ``52.43%``,别写 ``52.4346728%``"
        ),
    },

    # ------------------------------------------------------------------ #
    # K8s 集群巡检 / 体检 —— swarm 巡检的 k8s 对应物
    # ------------------------------------------------------------------ #
    # 一把口固化进 k8s_cluster_overview:节点(含 InternalIP 反查 Zabbix)+ 异常 pod
    # + 副本不匹配 deployment。triggers 跟 swarm 共享通用"集群巡检"词——靠 agent
    # 预路由的"集群类型兼容性"挑选(用户说 codewave→k8s 就选这个,sws-swarm→swarm
    # 就选 swarm 那个)。
    {
        "key": "cluster_health_audit_k8s",
        "title": "K8s 集群巡检 / 体检",
        "description": (
            "一键拉全:所有节点(含 InternalIP 反查的 Zabbix CPU/MEM/磁盘) + 全集群异常 pod "
            "(CrashLoop/Pending/重启高/拉镜像失败) + 副本不匹配的 deployment。"
            "解决「直接用 k8s node 名查 Zabbix 查不到」的痛点——从 node status.addresses 提 "
            "InternalIP 反查。"
        ),
        # 跟 swarm 共享通用巡检词 + k8s 限定词。集群类型由预路由 guard 区分。
        "triggers": [
            "k8s 巡检", "k8s 体检", "k8s 集群巡检", "kubernetes 巡检",
            "kube 巡检", "k8s 健康检查",
            "集群巡检", "集群体检", "集群健康检查",
            "整体看一下", "整体看下", "整体情况",
            "集群概况", "集群概览", "cluster audit", "health check",
        ],
        "inputs": [],
        "optional_inputs": ["lookback_hours"],
        "max_total_seconds": 240,
        "start_node": "overview",
        "nodes": {
            "overview": {
                "skill": "k8s_cluster_overview",
                "description": "一把口拉:节点+InternalIP+Zabbix 反查+异常 pod+副本不匹配 deployment",
                "args": {
                    "lookback_hours": "$user.lookback_hours||1",
                },
                "timeout_seconds": 180,
                "on_error": "fail",
            },
        },
        "final_report_prompt": (
            "你是 AI 运维助手。平台已经把 K8s 集群 raw 巡检数据采集回来"
            "(读 ``node_states[0].result`` 字段:``k8s`` / ``nodes[]`` / ``pods`` / ``deployments``)。\n"
            "**异常判定由你做**,平台只采集不判定。\n"
            "\n"
            "# 输出格式硬约束(必须按此结构,顺序/层级/格式一律不许偏离)\n"
            "\n"
            "## 📊 集群概览\n"
            "2-3 句:节点总数 / Ready 节点数 / 命名空间数 / 异常 pod 数 / 副本不匹配 deployment 数 / "
            "Zabbix 监控覆盖率(nodes 里 zabbix.found==True 的占比)。\n"
            "\n"
            "## 🖥️ 节点资源\n"
            "\n"
            "**直接输出 markdown 表格(不要用 ``` 代码块包裹,否则 chainlit 渲染成 raw code)**。表头:\n"
            "\n"
            "| 节点 | InternalIP | 角色 | Ready | CPU avg | CPU p95 | 内存% | 最高磁盘% | 备注 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "\n"
            "**所有节点都要列(N 节点 N 行,不许省略)**。取值:\n"
            "- **角色 / Ready**:``nodes[i].role`` / ``nodes[i].ready``\n"
            "- **CPU avg / p95**:``nodes[i].zabbix.overview.metric_summary.cpu_avg`` / ``cpu_p95`` (%)\n"
            "- **内存%**:``nodes[i].zabbix.overview.memory_summary.memory_used_percent``\n"
            "- **最高磁盘%**:从 ``nodes[i].zabbix.storage.filesystems[]`` 挑 ``used_percent`` 最大项,"
            "  写 ``<mount> N%``;``filesystems`` 空 或 ``zabbix.storage`` 不存在 → ``-``\n"
            "- **备注**:``ready != Ready`` → ``🚨 节点 NotReady``;数字 ≥90% → ``🚨 <字段>危急``;"
            "  ≥80% → ``⚠️ <字段>偏高``;``zabbix.found==False`` → ``❓ 未纳监控``(CPU/内存/磁盘写 ``-``);"
            "  全正常 → ``✅``\n"
            "\n"
            "## 🔴 异常 Pod\n"
            "\n"
            "读 ``pods.abnormal[]``。**这是固定段落,必须输出**;直接 markdown 表格(**不要 ``` 包裹**):\n"
            "\n"
            "| 命名空间 | Pod | 状态 | 节点 | 重启次数 | 原因 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "\n"
            "- **必须逐条列出 ``pods.abnormal[]`` 里的每一行,不许只写数字 / 不许概括 / 不许省略**\n"
            "- **原因**列取 ``reasons[]``(CrashLoopBackOff / ImagePullBackOff 等),空就写 phase\n"
            "- 若 ``pods.truncated == True``:表格末尾补一行 "
            "``> ⚠️ 异常 pod 较多,以上为前 N 条,共 abnormal_count 个,完整见调用 trace``\n"
            "- 表格下补:``其余 pod 运行正常``\n"
            "- ``pods.abnormal_count == 0`` → 直接写 ``✅ 全部 N 个 pod 运行正常``,**不输出空表格**\n"
            "\n"
            "## ⚙️ 副本不匹配 Deployment\n"
            "\n"
            "读 ``deployments.abnormal[]``。**只列 ready<desired**,markdown 表格(**不要 ``` 包裹**):\n"
            "\n"
            "| 命名空间 | Deployment | 期望 | 就绪 | 可用 | 问题 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "\n"
            "- **问题**:``ready==0`` → 🚨 全副本未就绪;``0<ready<desired`` → ⚠️ 副本不足\n"
            "- ``deployments.abnormal_count == 0`` → ``✅ 全部 deployment 副本正常``\n"
            "\n"
            "## 💡 总结 & 建议\n"
            "\n"
            "1-3 条 bullet:关键问题(NotReady 节点 / 高水位节点 / CrashLoop pod / 副本不足),"
            "每条配具体下一步(看 describe / 拉 logs / 扩容 / 加监控)。\n"
            "\n"
            "# 铁律(违反等于错)\n"
            "\n"
            "1. **表格 = 裸 markdown 表格**——前后不许加 ``` 包裹\n"
            "2. **节点表必须全列**——N 节点 N 行\n"
            "3. **字段路径要对**:磁盘走 ``zabbix.storage.filesystems``;字段不存在写 ``-``\n"
            "4. **该警告就警告**——CPU 85% 不许写正常,NotReady 节点不许漏\n"
            "5. **不许『无害化』概括**——「整体健康」这种没数据支撑的话一律不许出现\n"
            "6. **数字保留 2 位小数**"
        ),
    },
]


# 可被 seed 自动同步覆盖的 "平台所有" 标记。
#
#   - ``bootstrap``      首次 seed 落库时打的标记（initial insert）
#   - ``bootstrap-sync`` 后续重启发现 DEFAULTS 改了、自动同步覆盖时打的标记
#   - ``None`` / ``""``  老库里残留的 / 没标记 owner 的行，也视作平台所有
#
# 只要 ``updated_by`` 落在这个集合里，下次重启就会用 DEFAULTS 把这一行刷新；
# 任何其它值（``admin`` / ``alice`` / ...）都视作 admin 在后台改过，**永不覆盖**。
_PLATFORM_OWNED_MARKERS: frozenset = frozenset({"bootstrap", "bootstrap-sync", None, ""})


def seed_default_runbooks(store) -> dict[str, list[str]]:
    """seed / sync 默认 runbook 到 DB。

    语义（行为契约）：

      1. **DB 没有该 key** → 插入（``updated_by='bootstrap'``，记入 ``inserted``）
      2. **DB 有且 ``updated_by`` ∈ {``bootstrap``, ``bootstrap-sync``, None, ``""``}**
         （平台所有）→ 用 DEFAULTS 覆盖，``updated_by`` 改写为 ``bootstrap-sync``
         （表示这一行是平台自动同步过的，区别于首次 seed）。记入 ``synced``。
      3. **DB 有且 ``updated_by`` 是其它值**（admin 在后台改过）→ **绝不动**，
         记入 ``admin_skipped``。
      4. **优化**：如果 DEFAULTS 里的 definition dict 跟 DB 现存 definition 内容完全
         一致，跳过 upsert（免无意义 version bump 和 updated_at 刷新），记入
         ``unchanged``。

    这样：
      - 新增默认 runbook 不需要清表
      - 改 DEFAULTS（改 prompt / 改 trigger）只要重启进程就生效，不用再写一次性
        upsert 迁移脚本
      - admin 在管理后台编辑过的 runbook 不会被改回去

    日志：以 ``inserted=N synced=N admin-skipped=N unchanged=N`` 单行 INFO 落盘，
    方便 ``docker logs`` / ``grep`` 复核。

    Returns:
        ``{key: [errors] or []}`` —— 只列出本次"动过"（insert/sync）的 key。
    """
    existing: dict[str, dict] = {}
    try:
        for row in store.list_runbooks():
            key = row.get("key")
            if key:
                existing[key] = row
    except Exception as exc:  # pragma: no cover
        logger.warning("seed: list_runbooks 失败：%s", exc)
        return {}

    seeded: dict[str, list[str]] = {}
    inserted = 0
    synced = 0
    admin_skipped = 0
    unchanged = 0
    for definition in DEFAULT_RUNBOOKS:
        key = definition.get("key") or "?"
        row = existing.get(key)

        if row is not None:
            owner = row.get("updated_by")
            # admin 改过的，认主不动
            if owner not in _PLATFORM_OWNED_MARKERS:
                admin_skipped += 1
                continue
            # 平台所有，但 definition 已经一致 → 免 version bump
            if row.get("definition") == definition:
                unchanged += 1
                continue
            action = "sync"
        else:
            action = "insert"

        try:
            rb = load_runbook_from_dict(definition)
        except RunbookLoadError as e:
            seeded[key] = [f"load 失败：{e}"]
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
            # initial insert 打 'bootstrap'；后续 DEFAULTS 改了被覆盖的 → 'bootstrap-sync'
            updated_by="bootstrap" if action == "insert" else "bootstrap-sync",
        )
        seeded.setdefault(rb.key, [])
        if action == "insert":
            inserted += 1
            logger.info("seed: 插入默认 runbook %s", rb.key)
        else:
            synced += 1
            logger.info("seed: 同步默认 runbook %s（DEFAULTS 已变，覆盖 DB 旧版本）", rb.key)

    logger.info(
        "seed: inserted=%d synced=%d admin-skipped=%d unchanged=%d",
        inserted, synced, admin_skipped, unchanged,
    )
    return seeded
