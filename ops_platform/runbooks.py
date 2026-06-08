"""诊断剧本（runbook）库——把「运维老司机的查问题套路」沉淀成结构化数据。

模型在排障时调 ``platform_get_runbooks`` skill 拿到一份按场景索引的查询路径，减少
瞎撞、减少漏查。这里只放剧本数据，剧本的自动执行还有另一份 DAG 版本在
``runbook_seeds.py``（被 ``platform_run_runbook`` 执行）。

skill 体系重构 (2026-06,大道至简)
==================================
主机命令全部合并到**唯一执行口** ``host_run_command``(node + command,**每次执行
弹确认**,平台按 node 自动路由到所属集群)。原来的 host_query / host_kernel_events /
host_inspect_container_netns / host_capture_packets / host_run_command_async /
host_list_* 全删 —— 看监听端口 / 防火墙 / 路由 / 内核事件 / 进容器 netns,统统直接用
``host_run_command`` 跑对应命令(ss / iptables-save / ip route / dmesg / nsenter ...)。
只读查询底座仍在:

- ``kube_query``      —— 任意 ``kubectl get/describe/logs/top/events``
- ``swarm_query``     —— 任意 ``docker service/node/task ls/inspect/ps/logs``
- ``host_run_command`` —— 节点上任意命令(主机层,或 nsenter 进容器)

下面所有 step 都按这个写。
"""

from __future__ import annotations

from typing import Any


RUNBOOKS: dict[str, dict[str, Any]] = {
    "swarm_service_not_starting": {
        "title": "Swarm 服务起不来 / 持续重启",
        "triggers": ["起不来", "启动失败", "不停重启", "CrashLoop", "不断 restart"],
        "steps": [
            {
                "skill": "swarm_query",
                "args_hint": "category=service, verb=inspect, name=<服务名>",
                "why": "拿副本数、更新状态、env、镜像引用、约束的完整概况",
            },
            {
                "skill": "swarm_query",
                "args_hint": "category=service, verb=ps, name=<服务名>, filters={'desired-state':'failed'}",
                "why": "拿最近失败任务的 exit code / error message / Node。返回的 _signals 会自动识别 137=OOM / 1=配置/ 镜像拉取/权限错",
                "signal_to_next": {
                    "OOMKilled / exit code 137": "→ zabbix_get_host_overview(host_query=<task.Node>)",
                    "no space left / disk full / write failed": "→ zabbix_get_host_storage_overview",
                    "exit code 1 / 配置类错误": "→ swarm_query(verb=logs, filters={grep:'error', tail:500})",
                    "ImagePullBackOff / pull access denied": "→ swarm_query(verb=inspect) 看镜像引用 + registry 凭证",
                },
            },
            {
                "skill": "swarm_query",
                "args_hint": "category=service, verb=logs, name=<服务名>, filters={grep:'error', tail:500}",
                "why": "按关键词捞应用层日志（先 error、再 exception、再 timeout、再 OOM）",
                "tip": "filters.grep 多次换关键词；tail 默认 100，长服务可调 500",
            },
        ],
        "stop_when": "已能给出根因 + 证据 + 修复建议，或验证服务现已健康",
    },

    "swarm_service_slow_or_high_latency": {
        "title": "Swarm 服务慢 / 高延迟 / 偶发超时",
        "triggers": ["慢", "卡", "超时", "高延迟", "RT 飙高"],
        "steps": [
            {
                "skill": "swarm_query",
                "args_hint": "category=service, verb=inspect, name=<服务名>",
                "why": "先排除副本数掉了、更新中、重启循环",
            },
            {
                "skill": "swarm_query",
                "args_hint": "category=service, verb=logs, filters={grep:'timeout', tail:500}",
                "why": "用 timeout / slow / deadline / connection refused 多次换关键词过滤",
            },
            {
                "skill": "zabbix_get_host_overview",
                "why": "若日志显示外部依赖慢，再看宿主 CPU 负载 / 网络 / load average",
                "tip": "host_query 用 service 实际跑在哪台 Node 上的名字（从 swarm task 里取）",
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
                "next": "如果是 Swarm node 且最近有服务异常，往回看 swarm_query(verb=ls) / swarm_query(verb=ps)",
            },
        ],
    },

    "k8s_pod_crashloop": {
        "title": "K8s Pod CrashLoopBackOff / 不断重启",
        "triggers": ["CrashLoopBackOff", "Pod 重启", "OOMKilled", "Evicted"],
        "steps": [
            {
                "skill": "kube_query",
                "args_hint": "verb=get, resource=pods, namespace=<ns>, output=json",
                "why": "拿到 namespace 整体状态、谁出问题。output=json 让 scanner 自动识别 waiting_reasons 里的 CrashLoop/ImagePull",
            },
            {
                "skill": "kube_query",
                "args_hint": "verb=describe, resource=pod, name=<pod>",
                "why": "看 Events：OOMKilled / Evicted / FailedScheduling / ImagePullBackOff / 探针失败",
            },
            {
                "skill": "kube_query",
                "args_hint": "verb=logs, name=<pod>, previous=True",
                "why": "拉日志；**CrashLoop 必须 previous=true** 看上一次崩溃前的输出",
                "tip": "多容器 pod 用 container 参数指定",
            },
            {
                "signal_to_next": {
                    "OOMKilled": "→ kube_query(verb=describe) 看 limits/requests，必要时 zabbix_get_host_overview 看 Node",
                    "Evicted (DiskPressure)": "→ zabbix_get_host_storage_overview 该 node",
                    "FailedScheduling": "→ kube_query(verb=get, resource=nodes) 看节点污点 / 资源占用",
                    "ImagePullBackOff": "→ describe 里看完整镜像引用，确认 registry/secret",
                },
            },
        ],
    },

    "k8s_deploy_rollout_failed": {
        "title": "K8s 发布失败 / Rollout 卡住",
        "triggers": ["发布失败", "rollout", "更新卡住", "新版本不生效"],
        "steps": [
            {"skill": "kube_query", "args_hint": "verb=get, resource=deploy, namespace=<ns>, output=json",
             "why": "对比 desired/ready/available/updated 副本"},
            {"skill": "kube_query", "args_hint": "verb=get, resource=pods, selector='app=<name>'",
             "why": "找新 ReplicaSet 起来的 pod 是否健康"},
            {"skill": "kube_query", "args_hint": "verb=describe, resource=pod, name=<新 pod>",
             "why": "新 pod 如果失败，看 Events 找根因"},
            {"skill": "kube_query", "args_hint": "verb=logs, name=<新 pod>", "why": "新版本应用层错误"},
            {"stop_when": "定位到根因；如确认是新版本问题，可建议用户调 k8s_rollout_undo 回滚"},
        ],
    },

    "k8s_inspect_app_config": {
        "title": "查 K8s 应用配置（**优先级 ConfigMap > Secret > exec**）",
        "triggers": ["coredns 配置", "看 ConfigMap", "应用配置是什么", "配置文件", "环境变量"],
        "preface": (
            "**查 K8s 应用配置时，永远先 ConfigMap，再 Secret，最后才考虑 exec 进容器。**"
            "理由：99% 的配置文件挂载来源就是 ConfigMap/Secret，且：(1) 不依赖容器内有 cat/grep；"
            "(2) 可直接看到完整 YAML 不需要拼路径；(3) Pod 重建不会丢；(4) 性能更好。"
        ),
        "steps": [
            {
                "skill": "kube_query",
                "args_hint": "verb=get, resource=configmap, namespace=<ns>, name=<name>",
                "why": "直接看 ConfigMap 内容（包含 Corefile / config.yaml / 等 data 字段）",
                "tip": "想知道某 pod 挂了哪些 ConfigMap：先 kube_query(verb=describe, resource=pod) 看 Volumes 段",
            },
            {
                "skill": "kube_query",
                "args_hint": "verb=get, resource=secret, namespace=<ns>, name=<name>, output='jsonpath={.data}'",
                "why": "Secret 的 data 是 base64 编码；要用 jsonpath 提取，**敏感数据慎处理**",
            },
            {
                "next": "实在需要看容器内运行时配置（比如有 init container 注入的），才用 host_run_command（admin 审批）+ kubectl exec",
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
            "**不依赖 SSH 的网络排障**——全程用 ``host_run_command`` 在目标节点宿主机视角跑命令"
            "(每条弹确认)。要进**某容器**的网络视角,见 ``container_netns_diag``。"
        ),
        "steps": [
            {
                "skill": "host_run_command",
                "args_hint": "node=<node>, command='ss -ltnup'",
                "why": "宿主机视角看监听端口;先确认目标端口是不是真有进程在 listen",
            },
            {
                "skill": "host_run_command",
                "args_hint": "node=<node>, command=\"iptables-save | grep -E '<目标端口>|DROP|DNAT'\"",
                "why": "排查端口被 iptables/nft DROP / DNAT 错位 / kube-proxy 规则丢失;grep 收窄到目标端口",
                "signal_to_next": {
                    "KUBE-SERVICES 引用了不存在的 chain": "→ kube-proxy 异常,kube_query(verb=describe, resource=pod, name=<kube-proxy pod>)",
                    "大量 DOCKER-USER DROP": "→ Swarm/docker 网络 ACL 配置问题",
                },
            },
            {
                "skill": "host_run_command",
                "args_hint": "node=<node>, command='ip route; ip rule'",
                "why": "默认路由 / 多网卡选择 / overlay 接口(vxlan / cni0 / flannel.1)状态",
            },
            {
                "skill": "host_run_command",
                "args_hint": "node=<node>, command=\"dmesg -T 2>/dev/null | grep -iE 'conntrack|nf_|drop|retransmit' | tail -50\"",
                "why": "内核层:conntrack table full / nf_conntrack drop / TCP 重传 / NIC offload 错误(老 CentOS7 ``dmesg`` 无 -T 就去掉)",
            },
            {
                "skill": "host_run_command",
                "args_hint": "node=<node>, command='timeout 20 tcpdump -ni any port <端口> -c 200'",
                "why": "**最后兵器**——前面没结论时抓包确认。filter 务必精确,duration ≤ 30s,加 -c 限包数",
            },
        ],
        "stop_when": "已能给出「链路在哪一段断」的结论 + 关键证据(iptables 行 / dmesg 行 / 抓包结果)",
    },

    "container_netns_diag": {
        "title": "进容器看网络视角:容器里访问域名解析到哪个 IP / 容器连不连得通某地址",
        "triggers": [
            "容器里访问", "容器内解析", "容器 DNS", "容器里 ping",
            "容器连不连得通", "从容器看", "容器网络视角", "容器出网",
        ],
        "preface": (
            "问「**某容器里**访问域名 X 解析到哪个 IP / 连不连得通 Y」时,要进**那个容器自己的"
            "网络 namespace** 跑命令(宿主机的 DNS/路由跟容器可能不一样,直接在宿主机跑会得到错的答案)。"
            "全程 ``host_run_command``,两步:**定位节点 → 在该节点上 filter 拿容器 PID 再 nsenter**。\n"
            "**⚠️ Swarm 容器名的坑(最容易栽)**:真实容器名是 ``<服务>.<副本>.<任务ID>``"
            "(如 ``dm_prod_sws_bas_prod.1.wgvv0qyk7a4jh3mnbee5bf1o1``),而 ``docker service ps`` 给的是"
            "**任务名** ``<服务>.<副本>``(没有 .任务ID)。**绝不能拿任务名直接 ``docker inspect``** ——"
            "inspect 只认精确名 / ID 前缀,**不认名字前缀**,会报 ``No such object``。必须用 "
            "``docker ps --filter name=``(支持名字子串匹配)先拿到真实容器 ID。"
        ),
        "steps": [
            {
                "skill": "host_run_command",
                "args_hint": (
                    "Swarm: node=<任意管理节点>, command=\"docker service ps <服务名> "
                    "--filter desired-state=running --format '{{.Node}}'\" 看跑在哪个/哪些节点;"
                    "K8s: command='kubectl get pod <pod> -o wide' 看 NODE 列"
                ),
                "why": "**先定位容器在哪个节点**——Swarm 容器常跑在 worker,不在管理节点",
            },
            {
                "skill": "host_run_command",
                "args_hint": (
                    "**到上一步的节点上,一条命令搞定「拿 PID + 进 netns」**(末尾命令随意换 getent/nc/curl/cat):\n"
                    "Swarm: node=<那个节点>, command=\"PID=$(docker inspect -f '{{.State.Pid}}' "
                    "$(docker ps -q --filter name=<服务>.<副本> | head -1)); echo PID=$PID; "
                    "nsenter -t $PID -n getent hosts oss.chinasws.com\"\n"
                    "K8s(containerd): node=<那个节点>, command=\"PID=$(crictl inspect --output go-template "
                    "--template '{{.info.pid}}' $(crictl ps -q --pod $(crictl pods -q --name <Pod名> | head -1) | head -1)); "
                    "echo PID=$PID; nsenter -t $PID -n getent hosts oss.chinasws.com\""
                ),
                "why": (
                    "``docker ps --filter name=`` / ``crictl ps --name`` 都按名字**子串**匹配,能命中带 "
                    ".任务ID 后缀的真名 → 拿容器 ID → inspect 出宿主机 PID → ``nsenter -t $PID -n`` 进容器"
                    "**网络** namespace 跑命令。工具用宿主机的(getent/nslookup/nc/curl),没装就换一个"
                    "(``getent hosts`` ≈ nslookup)。"
                ),
                "tip": (
                    "① 看容器自己的 resolv.conf:加 ``-m`` 进 mount namespace —— "
                    "``nsenter -t $PID -n -m cat /etc/resolv.conf``;"
                    "② 连通性:把 ``getent hosts <域名>`` 换成 ``nc -zv <ip> <port>``;"
                    "③ 副本号 >9 的服务,给 filter 末尾加个点锚定(``name=<服务>.<副本>.``)避免 .1 误匹配 .10"
                ),
            },
        ],
        "stop_when": "已拿到容器网络视角下的解析结果 / 连通性结论 + 证据(IP / nc 返回)",
    },

    "node_health_audit": {
        "title": "节点深度健康检查（ad-hoc）",
        "triggers": ["这台节点有问题", "节点抖动", "Node NotReady", "怀疑硬件问题"],
        "steps": [
            {"skill": "zabbix_get_host_overview", "why": "先用 Zabbix 看 CPU/MEM/可用性的统计视图"},
            {"skill": "zabbix_get_host_storage_overview", "why": "看挂载点容量"},
            {
                "skill": "host_run_command",
                "args_hint": "node=<node>, command=\"dmesg -T 2>/dev/null | grep -iE 'oom|i/o error|mce|edac|hardware' | tail -50\"",
                "why": "Zabbix 看不到的内核层事件(OOM / IO error / 硬件错误);老 CentOS7 ``dmesg`` 无 -T 就去掉",
            },
            {
                "skill": "host_run_command",
                "args_hint": "node=<node>, command='du -sh /var/lib/* 2>/dev/null | sort -rh | head'  /  'systemctl status docker kubelet'",
                "why": "**逃生口**——任意主机命令(查大目录 / 看服务状态 / journalctl 等),每条弹确认",
            },
        ],
    },
}


GENERAL_GUIDANCE = {
    "cross_domain_pivot_rule": (
        "诊断容器/Pod 问题时，**永远要追问一句「宿主机本身是否正常」**。"
        "Swarm task 的 Node、K8s pod 的 spec.nodeName 都可以作为 zabbix 的 host_query。"
        "如果用户最初只问应用问题，不要在没有证据的情况下把锅甩给主机；但只要任务 / 日志 / 事件中"
        "出现 OOM / disk / evicted / DiskPressure / no space / connection refused 等关键词，"
        "**必须**追加一次 zabbix 主机概览或磁盘概览查询。"
    ),
    "skill_inventory_v2": (
        "**大道至简的 skill 架构**：\n"
        "- 只读查询:kube_query / swarm_query —— k8s/swarm 的只读查询走这俩\n"
        "- **唯一命令执行口:host_run_command** —— 节点上任意命令(主机层 ss/ip/iptables/dmesg/df,"
        "或 nsenter 进容器),**每次执行弹确认**,平台按 node 自动路由集群。原来的 host_query / "
        "host_kernel_events / host_inspect_container_netns / host_capture_packets / "
        "host_run_command_async / host_list_* 全删 —— 统统用 host_run_command 跑命令\n"
        "- 监控:zabbix_get_host_* / metric_query / *_cluster_overview\n"
        "- 写操作:k8s_scale_deployment / swarm_remove_service / 等\n"
        "**不要再调已删的名字**(host_query / host_kernel_events / host_list_nodes / "
        "k8s_list_pods / swarm_get_service_detail 等)。"
    ),
    "pivot_keys": {
        "swarm_task → zabbix_host": "swarm_query(verb=ps) 的 Node 列 → zabbix host_query",
        "k8s_pod → zabbix_host": "kube_query(verb=get, resource=pods, output=json) 的 spec.nodeName → zabbix host_query",
        "service_name → swarm_logs": "在 swarm_query(verb=logs) 里逐次换 filters.grep",
        "host_resource → 内核事件": (
            "Zabbix 看不到的内核层异常(OOM / IO error / conntrack)走 "
            "host_run_command(command=\"dmesg -T 2>/dev/null | grep -iE 'oom|conntrack|i/o error'\")"
        ),
        "node_name → host_run_command": (
            "怀疑端口被防火墙拦时,从 swarm/k8s 拿到 Node 名后 host_run_command(command='ss -ltnup') "
            "+ host_run_command(command=\"iptables-save | grep <端口>\")"
        ),
        "want_app_config": (
            "想看 K8s 应用配置（如 coredns Corefile）：**先 ConfigMap**——"
            "kube_query(verb=get, resource=configmap, name=...)，不要进容器 cat"
        ),
    },
    "ssh_replacement_note": (
        "本平台默认禁止 SSH。所有「进宿主机查」的需求统一走 host_agent connection + "
        "**host_run_command**(节点上任意命令,每次弹确认,按 node 自动路由集群)。底层是部署在"
        "每个节点的特权 agent 容器(mode:global / DaemonSet)。"
        "如果 host_run_command 报该 node 没有 agent,说明 agent 还没部署,参考 docs/host-agent.md。"
    ),
    "stop_conditions": [
        "已经能给出 当前状态 + 检测过程 + 关键证据 + 判断结论 + 建议操作",
        "已重试同一 skill ≥ 2 次仍然没新信息（避免死循环）",
        "进入写操作 needs_confirmation 流程后立即停止 tool 循环，等待用户确认",
    ],
}
