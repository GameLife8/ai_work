"""诊断剧本（runbook）库：把「运维老司机的查问题套路」沉淀成结构化数据。

模型在排障时可以调 ``platform_get_runbooks`` skill 拿到一份按场景索引的查询路径，
减少模型瞎撞、减少漏查。这里只放剧本数据，不放代码逻辑——剧本本身的执行还是靠
模型按 step 自己挑 skill 调用。

新增剧本只要往 RUNBOOKS 里加一项即可，无需改其它文件。
"""

from __future__ import annotations

from typing import Any


RUNBOOKS: dict[str, dict[str, Any]] = {
    "swarm_service_not_starting": {
        "title": "Swarm 服务起不来 / 持续重启",
        "triggers": ["起不来", "启动失败", "不停重启", "CrashLoop", "不断 restart"],
        "steps": [
            {
                "skill": "swarm_check_service_health",
                "why": "拿当前副本数、更新状态、重启策略、最近 5 条任务的概况",
            },
            {
                "skill": "swarm_get_failed_tasks",
                "why": "拿最近失败任务的 exit code / error message / Node",
                "signal_to_next": {
                    "OOMKilled / exit code 137": "→ 调用 zabbix_get_host_overview(host_query=<task.Node>) 看宿主机内存",
                    "no space left / disk full / write failed": "→ zabbix_get_host_storage_overview(host_query=<task.Node>)",
                    "exit code 1 / 配置类错误": "→ swarm_get_service_logs_filter 用 error/exception/config/permission 过滤",
                    "ImagePullBackOff / pull access denied": "→ swarm_get_service_detail 看镜像引用，确认 registry 凭证",
                },
            },
            {
                "skill": "swarm_get_service_logs_filter",
                "why": "按关键词捞应用层日志（先 error、再 exception、再 timeout、再 OOM）",
                "tip": "多次调用，每次换一个 keyword；tail 默认 100 通常够用",
            },
            {
                "skill": "swarm_get_service_detail",
                "why": "确认镜像 tag、env、volume、约束。环境变量空格/拼写错误是常见根因",
            },
        ],
        "stop_when": "已经能给出根因 + 证据 + 修复建议，或者已经验证服务现在健康",
    },

    "swarm_service_slow_or_high_latency": {
        "title": "Swarm 服务慢 / 高延迟 / 偶发超时",
        "triggers": ["慢", "卡", "超时", "高延迟", "RT 飙高"],
        "steps": [
            {"skill": "swarm_check_service_health", "why": "先排除副本数掉了、更新中、重启循环"},
            {
                "skill": "swarm_get_service_logs_filter",
                "why": "用 timeout / slow / deadline / connection refused 过滤",
            },
            {
                "skill": "zabbix_get_host_overview",
                "why": "若日志显示外部依赖慢，再看宿主 CPU 负载 / 网络/ load average",
                "tip": "host_query 用 service 实际跑在哪台 Node 上的名字（从 swarm 任务里取）",
            },
        ],
    },

    "host_resource_alert": {
        "title": "主机告警：CPU / 内存 / 磁盘 / 可用性",
        "triggers": ["主机CPU高", "内存高", "磁盘满", "宕机", "ping 不通", "Zabbix 告警"],
        "steps": [
            {"skill": "zabbix_get_host_overview", "why": "整体可用性、CPU、内存、agent 状态"},
            {"skill": "zabbix_get_host_storage_overview", "why": "细看每个挂载点用率，定位是哪一块满"},
            {
                "next": "如果这台主机是 Swarm node，并且最近有服务异常，往回看 swarm_list_services / swarm_get_failed_tasks",
            },
        ],
    },

    "k8s_pod_crashloop": {
        "title": "K8s Pod CrashLoopBackOff / 不断重启",
        "triggers": ["CrashLoopBackOff", "Pod 重启", "OOMKilled", "Evicted"],
        "steps": [
            {"skill": "k8s_list_pods", "why": "拿到 namespace 下整体状态、谁出问题，注意 waiting_reasons"},
            {
                "skill": "k8s_describe_pod",
                "why": "看 Events：OOMKilled / Evicted / FailedScheduling / ImagePullBackOff / 探针失败",
            },
            {
                "skill": "k8s_get_pod_logs",
                "why": "拉日志；CrashLoop 时务必带 previous=true 看上一次崩溃前的输出",
                "tip": "多容器 pod 用 container 参数指定",
            },
            {
                "signal_to_next": {
                    "OOMKilled": "→ 调 k8s_describe_pod 看 limits/requests，必要时 zabbix_get_host_overview 看 Node",
                    "Evicted (DiskPressure)": "→ zabbix_get_host_storage_overview 该 node",
                    "FailedScheduling": "→ k8s_list_pods 看节点污点 / 资源占用",
                    "ImagePullBackOff": "→ describe 里看完整镜像引用，确认 registry/secret",
                },
            },
        ],
    },

    "k8s_deploy_rollout_failed": {
        "title": "K8s 发布失败 / Rollout 卡住",
        "triggers": ["发布失败", "rollout", "更新卡住", "新版本不生效"],
        "steps": [
            {"skill": "k8s_list_deployments", "why": "对比 desired/ready/available/updated 副本"},
            {"skill": "k8s_list_pods", "why": "找新 ReplicaSet 起来的 pod 是否健康"},
            {"skill": "k8s_describe_pod", "why": "新 pod 如果失败，看 Events 找根因"},
            {"skill": "k8s_get_pod_logs", "why": "新版本应用层错误"},
            {
                "stop_when": "定位到根因；如确认是新版本问题，可建议用户调 k8s_rollout_undo 回滚",
            },
        ],
    },

    "alert_payload_triage": {
        "title": "原始告警 payload 研判",
        "triggers": ["这条告警", "JSON payload", "帮我看下这个报警"],
        "steps": [
            {"skill": "alerts_analyze_payload", "why": "解析 payload + 自动补全上下文 + 给优先级建议"},
            {"next": "若结果指向某主机/服务异常，可继续按 host_resource_alert 或 swarm_service_not_starting 展开"},
        ],
    },

    "network_troubleshooting": {
        "title": "网络不通 / 端口连不上 / DNAT 异常 / 包丢失",
        "triggers": [
            "连不上", "ping 不通", "telnet 不通", "端口不通", "网络抖动",
            "丢包", "iptables", "DNAT", "overlay 异常", "service VIP",
        ],
        "preface": (
            "**这是不依赖 SSH 的网络排障路径**——通过部署在每节点的 ai-ops-agent 容器，"
            "走 nsenter 进宿主机 namespace 取证。第一步永远先 host_list_nodes 拿到目标节点名。"
        ),
        "steps": [
            {
                "skill": "host_list_nodes",
                "why": "确定 host_agent 已经覆盖目标集群，并拿到 node 列表",
            },
            {
                "skill": "host_socket_overview",
                "why": "宿主机视角看监听端口；先确认目标端口是不是真的有进程在 listen",
                "tip": "filter 参数可传 ':80' / ':443' / 'docker' / 'kube-proxy' 缩小输出",
            },
            {
                "skill": "host_iptables_dump",
                "why": "排查端口被 iptables/nft DROP / DNAT 错位 / kube-proxy 规则丢失",
                "signal_to_next": {
                    "看到 chain KUBE-SERVICES 引用了不存在的 chain": "→ kube-proxy 异常，去看 K8s describe pod kube-proxy",
                    "看到大量 DOCKER-USER DROP": "→ Swarm/docker 的网络 ACL 配置问题",
                },
            },
            {
                "skill": "host_route_overview",
                "why": "确认默认路由 / 多网卡选择 / overlay 接口（vxlan / cni0 / flannel.1）状态",
            },
            {
                "skill": "host_inspect_container_netns",
                "why": "上面是宿主机视角；如果是某个容器内连不出去，要进它自己的 netns 再查一遍",
            },
            {
                "skill": "host_kernel_events",
                "why": "看 conntrack table full / nf_conntrack drop / TCP retransmit / NIC offload 错误",
                "tip": "keyword 试 'conntrack' / 'drop' / 'nf_'",
            },
            {
                "skill": "host_capture_packets",
                "why": "**最后兵器**——前面没结论时抓包确认。filter 务必精确，duration ≤ 30s",
            },
        ],
        "stop_when": "已经能给出「链路在哪一段断」的结论 + 关键证据（iptables 行 / dmesg 行 / 抓包结果）",
    },

    "node_health_audit": {
        "title": "节点深度健康检查（ad-hoc）",
        "triggers": ["这台节点有问题", "节点抖动", "Node NotReady", "怀疑硬件问题"],
        "steps": [
            {"skill": "host_list_nodes", "why": "确认 agent 覆盖到目标节点"},
            {"skill": "zabbix_get_host_overview", "why": "先用 Zabbix 看 CPU/MEM/可用性的统计视图"},
            {"skill": "zabbix_get_host_storage_overview", "why": "看挂载点容量"},
            {
                "skill": "host_kernel_events",
                "why": "Zabbix 看不到的内核层事件（OOM / IO error / 硬件错误）",
                "tip": "keyword 试 'oom' / 'i/o error' / 'mce' / 'edac' / 'temperature'",
            },
            {
                "skill": "host_run_command",
                "why": "**逃生口**——只有上面都不够时才用，admin 审批；常见命令 systemctl status docker/kubelet / iostat / vmstat / df -h /var/lib",
            },
        ],
    },
}


GENERAL_GUIDANCE = {
    "cross_domain_pivot_rule": (
        "诊断容器/Pod 问题时，**永远要追问一句「宿主机本身是否正常」**。"
        "Swarm 任务输出里的 Node、K8s pod 的 spec.nodeName 都可以作为 zabbix 的 host_query。"
        "如果用户最初只问应用问题，不要在没有证据的情况下把锅甩给主机；但只要任务 / 日志 / 事件中"
        "出现 OOM / disk / evicted / DiskPressure / no space / connection refused 等关键词，"
        "**必须**追加一次 zabbix 主机概览或磁盘概览查询。"
    ),
    "pivot_keys": {
        "swarm_task → zabbix_host": "swarm_get_failed_tasks 的 Node 列 → zabbix host_query",
        "k8s_pod → zabbix_host": "k8s_list_pods 的 node 字段 → zabbix host_query",
        "service_name → swarm_logs": "在 swarm_get_service_logs_filter 里逐次换 keyword",
        "host_resource → host_kernel_events": (
            "Zabbix 看不到的内核层异常（OOM / IO error / conntrack）走 host_kernel_events"
        ),
        "node_name → host_socket_overview": (
            "怀疑端口被防火墙拦时，从 swarm/k8s 拿到 Node 名后调 host_socket_overview / host_iptables_dump"
        ),
    },
    "ssh_replacement_note": (
        "本平台默认禁止 SSH。所有「进宿主机查」的需求统一走 host_agent connection + host_* skill，"
        "底层是部署在每个节点的特权 agent 容器（mode:global / DaemonSet）。"
        "如果 host_list_nodes 返回为空，说明 agent 还没部署，参考 docs/host-agent.md。"
    ),
    "stop_conditions": [
        "已经能给出 当前状态 + 检测过程 + 关键证据 + 判断结论 + 建议操作",
        "已重试同一 skill ≥ 2 次仍然没新信息（避免死循环）",
        "进入写操作 needs_confirmation 流程后立即停止 tool 循环，等待用户确认",
    ],
}
