# Host Agent —— 用 DaemonSet/global 替代 SSH 排障

> 本文档对应代码：[`services/host_agent_client.py`](../services/host_agent_client.py)、
> [`ops_platform/drivers/host_agent.py`](../ops_platform/drivers/host_agent.py)、
> [`skills/host_*`](../skills/) 系列、[`deploy/`](../deploy/) 部署清单。

---

## 1. 它解决什么问题

私有化部署的运维团队经常面对：

- **SSH 不允许开**：合规、堡垒机、网段隔离都让 SSH 进宿主机变得越来越麻烦。
- **AI 排障要"进主机看"**：服务起不来到底是不是 iptables 拦了？conntrack 满了？OOM 了？只在容器里看不到。
- **审计要求**：人手敲的命令谁都说不清做了什么；用 SSH 敲完关掉 session 就没了。

**`host_agent` 把"进宿主机"这件事变成一次容器操作**：每个节点一个特权诊断容器（K8s 是 DaemonSet、Swarm 是 `mode: global`），平台通过集群 API（`kubectl exec` / `docker exec`）进入这个容器，再用 `nsenter` 进入宿主机的 namespace 跑命令。

走完这一圈：

| 维度 | SSH | host_agent |
|---|---|---|
| 鉴权 | 每台机器独立 authorized_keys | 集群 API 统一 RBAC |
| 命令审计 | 自己上 auditd | 平台 `platform_skill_call` 表自动记 |
| 网络入口 | 22 端口要开到运维 | 集群 API 一个口子 |
| 节点扩缩容 | 加 SSH 配置 | DaemonSet 自动覆盖 |
| 范围控制 | "全 root 或全没" | 命令白名单 + 审批 |

---

## 2. 它能查什么

部署 agent 后，平台多出 7 个 skill（详见 [skill 注册表](../skills/) 或登录后台 `/skills` 页面）：

| skill code | 类型 | 干啥 |
|---|---|---|
| `host_list_nodes` | 读 | 列出已部署 agent 的节点；**先调它拿到 node 名** |
| `host_socket_overview` | 读 | `nsenter ... ss -tunlp` 宿主机所有监听端口 |
| `host_iptables_dump` | 读 | iptables-save / nft list ruleset / ipvsadm |
| `host_route_overview` | 读 | ip a + ip route + ip rule + ip netns |
| `host_kernel_events` | 读 | dmesg + 关键词过滤（OOM / conntrack / I/O error 等） |
| `host_inspect_container_netns` | 读 | 给定容器名，进它自己的网络 namespace 查 socket / 路由 |
| `host_capture_packets` | **写** | tcpdump 抓包 N 秒；进入二次确认流 |
| `host_run_command` | **写**（admin 审批） | 任意命令逃生口；只有 admin 能确认 |

跨域协同（在 [`ops_platform/runbooks.py`](../ops_platform/runbooks.py) 已经登记成 runbook）：

- **`network_troubleshooting`**：`host_list_nodes` → `host_socket_overview` → `host_iptables_dump` → `host_route_overview` → `host_inspect_container_netns` → `host_kernel_events` → `host_capture_packets`
- **`node_health_audit`**：`host_list_nodes` → `zabbix_get_host_overview` → `zabbix_get_host_storage_overview` → `host_kernel_events` →（兜底）`host_run_command`

模型在面对"网络不通 / 主机抖动"问题时会先调 `platform_get_runbooks(name="network_troubleshooting")` 拿到这个剧本，再按 step 取证。

---

## 3. 怎么部署

### 3.1 K8s（推荐）

```bash
kubectl apply -f deploy/ai-ops-agent-k8s.yaml
kubectl -n ai-ops get pods -o wide -l app=ai-ops-agent
```

确认每个节点都起来了：

```bash
kubectl -n ai-ops get pods -l app=ai-ops-agent -o wide
# NAME                READY   STATUS    NODE
# ai-ops-agent-x7nhk  1/1     Running   it-cluster01-master
# ai-ops-agent-q3vzm  1/1     Running   it-cluster01-w01
# ai-ops-agent-...    1/1     Running   ...
```

### 3.2 Swarm

```bash
docker stack deploy -c deploy/ai-ops-agent-swarm.yml ai-ops
docker service ps ai-ops_ai-ops-agent
```

⚠️ **Swarm 的特殊限制**：`docker exec` 只能对本地 daemon 上的容器生效。要让平台真正跨节点 exec：

- **方案 A（推荐）**：每个节点的 daemon 暴露 `tcp://NODE:2375`（**必须配 mTLS**），平台对每个节点单独注册一份 `host_agent` connection（kind=swarm, docker_host=tcp://NODE:2375）。每份 connection 可以打 alias 区分（如 `swarm-prod-node1`）。
- **方案 B**：只在 manager 节点用 host_agent；这种情况下你只能 exec 到 manager 自己的 agent，看到 manager 这一台。

K8s 没这个问题——`kubectl exec` 走 API server 路由到任意 node，单个 connection 即可。

---

## 4. 怎么接入平台

### 4.1 admin 后台添加 connection

登录管理后台 → **接入管理** → **+ 新增接入**：

| 字段 | K8s 模式 | Swarm 模式 |
|---|---|---|
| 类型 | `节点诊断 Agent` | `节点诊断 Agent` |
| kind | `k8s` | `swarm` |
| Kubeconfig | 粘贴目标集群完整 kubeconfig | — |
| Agent namespace | `ai-ops`（与 yaml 一致） | — |
| Agent label selector | `app=ai-ops-agent` | — |
| DOCKER_HOST | — | `tcp://manager:2375` 或单节点 |
| Agent service 名 | — | `ai-ops_ai-ops-agent`（默认）|

保存后点"验证连通"——后端会调 `host_list_nodes` 探活，能列出节点就说明全链路通了。

### 4.2 在聊天里使用

打开 Chainlit，问题示例：

> *"dmz-cluster01 的 worker-3 节点上 80 端口没人监听，帮我查一下"*

模型典型行为：

```
1. platform_list_connections(type_code="host_agent")     # 找到 host_agent connection
2. host_list_nodes                                       # 确认 worker-3 在列表里
3. host_socket_overview(node="worker-3", filter=":80")   # 确认监听情况
4. host_iptables_dump(node="worker-3", mode="iptables")  # 看防火墙
5. host_route_overview(node="worker-3")                  # 看路由
→ 五段式中文报告
```

如果要抓包（写操作）：

> *"在 worker-3 上抓 10 秒去 1.2.3.4:443 的包"*

模型会调 `host_capture_packets`，平台返回 `needs_confirmation` token，**你在聊天卡片或后台"写操作待确认"页面点"确认执行"**，平台才真的跑 tcpdump。

### 4.3 通过 MCP server 给外部 agent 用

`platform_list_skills` 已经把这 7 个 skill 暴露给任何接 MCP 的客户端（Claude Code / Cursor / 其它）。
鉴权 token 在 `.env` 的 `MCP_API_KEYS` 里设。详见 [`docs/platform.md`](platform.md) MCP 章节。

---

## 5. 安全模型

### 5.1 谁能用这些 skill

| skill | 普通用户 | admin |
|---|---|---|
| `host_list_nodes` 等 5 个读 skill | ✅ | ✅ |
| `host_capture_packets`（写但不强审批） | ✅ 但需自己确认 | ✅ 需确认 |
| `host_run_command`（任意命令） | ❌ 不可见 | ✅ 必须 admin 二次确认 |

`host_run_command` 的 manifest 里：

```python
"read_only": False,
"requires_admin_approval": True,
"visibility": "admin",
```

**三重锁**：visibility 让普通用户的 chat 会话连这个 tool 都看不到（后端不喂给模型）；requires_admin_approval 强制只有 admin 能 confirm；正常的 needs_confirmation 流给一次"我要做 X，请确认"的人工拦截。

### 5.2 审计

每次 `host_*` skill 调用都进 `platform_skill_call` 表：
- `skill_code`、`connection_id`、`session_id`、`user`
- `args_json` 里完整的 node + 命令
- `result_json` 里 stdout/stderr 截断片段（避免吐 GB 级 tcpdump 输出）
- 写操作还会带 `_extra.confirmation_token` 反查到底是谁批准的

后台 **调用审计** 页面（admin 可见）按时间倒序展示。

### 5.3 攻击面

老实承认：拿到一份 host_agent connection 等于拿到了集群所有节点的 root。这跟"拿到 SSH 私钥"是同一级风险，不是更弱。**该做的安全**：

1. **JWT secret 强随机**（`ADMIN_JWT_SECRET`）；admin 账号开 MFA（这一版还没做，下一步建议）
2. **Connection 凭证字段加密**（这是平台的 known TODO；当前明文落 `platform_connection.config_json`）
3. **审计日志外推到 SIEM**（写一个 hook 把 `platform_skill_call` 投递到 syslog/ELK）
4. **AppArmor / SELinux**：宿主机 enforcing 时 nsenter 可能被拒，agent 容器需要 `--security-opt label=disable`，部署前先在测试节点验证
5. **网络隔离**：agent namespace 只对平台 backend 可见，不要暴露 API 给业务 namespace

---

## 6. 故障排查

### 6.1 `host_list_nodes` 返回空

- K8s：`kubectl -n ai-ops get pods -l app=ai-ops-agent` 看 pod 是不是 Running；node taint 没容忍会漏节点
- Swarm：`docker service ps ai-ops_ai-ops-agent` 看任务状态；mode:global 没生效会只起一个

### 6.2 `host_socket_overview` 报 `nsenter: cannot open /proc/1/ns/...`

agent 没拿到 hostPID。检查：
- K8s yaml 里的 `hostPID: true` / `hostNetwork: true` / `securityContext.privileged: true`
- Swarm yml 里的 `pid: "host"` / `network_mode: "host"` / `privileged: true`

### 6.3 `host_inspect_container_netns` 找不到容器

skill 现在按 **docker → crictl → ctr** 顺序探测，三种 runtime 都支持：

| 集群形态 | 用什么 | yaml 需要 |
|---|---|---|
| 老 K8s（≤ 1.23）+ Docker shim | `docker inspect` | 挂 `/var/run/docker.sock` ✅（默认） |
| K8s 1.24+ containerd | `crictl ps + crictl inspect` | 挂 `/run/containerd/containerd.sock` ✅（默认） + agent 镜像里要有 `crictl` |
| K8s 1.24+ CRI-O | `crictl` | 同上，env `CONTAINER_RUNTIME_ENDPOINT` 改 CRI-O socket |
| Swarm | `docker inspect` | 挂 `/var/run/docker.sock` ✅ |

**netshoot 默认不带 crictl**。三种解法：

**A. 用扩展镜像（推荐）**：自己 build 一份 `your-registry/netshoot-crictl:1.0`：

```dockerfile
FROM nicolaka/netshoot:latest
ARG CRICTL_VERSION=v1.30.0
RUN curl -fsSL "https://github.com/kubernetes-sigs/cri-tools/releases/download/${CRICTL_VERSION}/crictl-${CRICTL_VERSION}-linux-amd64.tar.gz" \
        | tar -xz -C /usr/local/bin
```

把 yaml 里的 `image: nicolaka/netshoot:latest` 替换成它即可。

**B. 用 initContainer 在线装**：[`deploy/ai-ops-agent-k8s.yaml`](../deploy/ai-ops-agent-k8s.yaml) 里有注释模板，取消注释、改下载源即可。

**C. 通过 ctr 兜底**：containerd 自带 `ctr`。skill 实现里已经做 `_try_ctr` 兜底，但 `ctr` 一般不在 netshoot 镜像里，所以 A/B 仍是首选。

### 6.4 `host_run_command` 一直 needs_confirmation 不执行

正常。它强制 admin 审批：
1. 普通用户调 → 落 pending
2. admin 进后台 **写操作待确认** 页面 → 看 args.command → 确认/拒绝
3. 平台执行，模型给最终总结

### 6.5 SELinux/AppArmor 拒绝 nsenter

`dmesg | grep -i denied` 看具体策略。常用法：
- SELinux：`setenforce 0` 临时验证，正式生产写 policy
- AppArmor：在 K8s yaml 里加 `container.apparmor.security.beta.kubernetes.io/agent: unconfined`

---

## 7. 限制 + Roadmap

当前实现已经够 95% 场景用，但留几个口子：

| 限制 | 影响 | 计划 |
|---|---|---|
| Swarm 跨节点 exec 需要 `tcp://NODE:2375` | 必须每节点单独注册 connection | 自定义 HTTP node-agent（详见 platform.md 路线图） |
| 抓包只返回文本头 | 大流量分析不友好 | 落到平台对象存储/MinIO，返回下载链接 |
| 审计未外发 | 需要 SIEM 时要自己写 hook | invoker 加 webhook hook 接口 |

✅ **已收尾**：
- containerd 适配：skill 已按 docker → crictl → ctr 三段探测，K8s 1.24+ 集群可用（agent 镜像需包含 crictl，见 6.3）
- 凭证加密：`platform_connection.config_json` 和 `platform_model_config.api_key` 落库前 Fernet 加密；首启自动迁移已有明文行，详见 [`platform.md` § 数据持久化](platform.md)

---

## 8. 一句话

> SSH 是工具，不是必需品。`host_agent` 让"进宿主机查"变成一次有审计、有权限、有审批、可被 AI 协同调度的标准动作。
