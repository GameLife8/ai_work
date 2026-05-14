# Host Agent —— 用节点 HTTP 代理替代 SSH 排障

> 本文档对应代码：
> [`agent/`](../agent/)（agent 主体），
> [`services/host_agent_client.py`](../services/host_agent_client.py)（平台侧客户端），
> [`ops_platform/drivers/host_agent.py`](../ops_platform/drivers/host_agent.py)，
> [`skills/host_*`](../skills/) 系列，
> [`deploy/`](../deploy/) 部署清单。

---

## 1. 它解决什么问题

私有化部署的运维团队经常面对：

- **SSH 不允许开**：合规、堡垒机、网段隔离都让 SSH 进宿主机越来越麻烦。
- **AI 排障要"进主机看"**：服务起不来到底是不是 iptables 拦了？conntrack 满了？OOM 了？只在容器里看不到。
- **审计要求**：人手敲的命令谁都说不清做了什么；用 SSH 敲完关掉 session 就没了。

**`host_agent` 把"进宿主机"这件事变成一次 HTTP 调用**：

每个节点跑一个特权 HTTP 代理容器（K8s DaemonSet / Swarm `mode: global`），监听节点的 `:9100`。平台 backend 直接 HTTP POST 到 `<node-ip>:9100/v1/exec`，agent 用 `nsenter -t 1` 进宿主机 namespace 执行命令，返回 JSON。

| 维度 | SSH | host_agent (v2 HTTP) |
|---|---|---|
| 鉴权 | 每台机器独立 authorized_keys | 集群级 Bearer token，每节点 docker secret / k8s Secret 分发 |
| 命令审计 | 自己上 auditd | 平台 `platform_skill_call` 表自动记 + agent 侧请求日志 |
| 网络入口 | 22 端口要开到运维 | 单一 `:9100`，可加 source CIDR 限制 |
| 节点扩缩容 | 加 SSH 配置 | DaemonSet/global 自动覆盖 |
| 范围控制 | "全 root 或全没" | agent 内置命令白名单 + 平台侧二次审批 |
| 流式输出 | tail -f 直接出 | `/v1/exec/stream` SSE，tcpdump / 滚日志原生支持 |

> **跟旧版（v1）的区别**：v1 让 agent 容器跑 `sleep infinity`，平台靠 `docker exec` / `kubectl exec` 进去再 `nsenter`。问题：`docker exec` 是 daemon-local 操作，跨节点必须 worker 也暴露 daemon TCP（违背"少暴露端口"初衷）。v2 让 agent 自己当 HTTP server，平台直连，**worker 不再需要任何 daemon socket 暴露**。

---

## 2. 它能查什么

部署 agent 后，平台多出 8 个 host_* skill：

| skill code | 类型 | 干啥 |
|---|---|---|
| `host_list_nodes` | 读 | 列出已部署 agent 的节点；**先调它拿到 node 名** |
| `host_socket_overview` | 读 | `ss -tunlp` 宿主机所有监听端口 |
| `host_iptables_dump` | 读 | iptables-save / nft list ruleset / ipvsadm |
| `host_route_overview` | 读 | `ip a` + `ip route` + `ip rule` + `ip netns` |
| `host_kernel_events` | 读 | `dmesg` + 关键词过滤（OOM / conntrack / I/O error 等） |
| `host_inspect_container_netns` | 读 | 给定容器名，进它自己的网络 namespace 查 socket / 路由 |
| `host_capture_packets` | **写** | `tcpdump` 抓包 N 秒；进入二次确认流 |
| `host_run_command` | **写**（admin 审批） | 任意（白名单内）命令逃生口；只有 admin 能确认 |

---

## 3. 协议规范

### 3.1 端点

| Method | Path | 鉴权 | 用途 |
|---|---|---|---|
| `GET` | `/livez` | ❌ | docker/k8s healthcheck 探活，**不带任何信息** |
| `GET` | `/v1/health` | ✅ | 详细健康（node 名、agent 版本、uptime、exec 计数、白名单）|
| `POST` | `/v1/exec` | ✅ | 一次性命令（JSON in/out）|
| `POST` | `/v1/exec/stream` | ✅ | 长命令（JSON in / SSE out）|

### 3.2 请求体

```json
{
  "cmd":              ["ss", "-ltnp"],
  "nsenter":          "muinp",
  "timeout_sec":      30,
  "max_output_bytes": 1048576
}
```

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `cmd` | ✅ | — | 数组形式，**不是** shell 字符串。第一个元素是命令名（basename），其余是参数。 |
| `nsenter` | ❌ | `"muinp"` | 进哪些 namespace。`m=mount u=uts i=ipc n=net p=pid U=user C=cgroup`。**空串** = 不进 host ns（在 agent 容器里跑）。 |
| `timeout_sec` | ❌ | 30 | 单次命令超时；上限 300。超时 = SIGTERM → 2s 后 SIGKILL。 |
| `max_output_bytes` | ❌ | 1 MB | stdout/stderr 各自的截断阈值；上限 10 MB。 |

### 3.3 `/v1/exec` 响应

```json
{
  "exit_code":   0,
  "stdout":      "State  Recv-Q  Send-Q ...",
  "stderr":      "",
  "duration_ms": 87,
  "truncated":   false,
  "timeout":     false
}
```

- 命令执行**完成**（即便 exit_code != 0）都是 HTTP 200。
- HTTP 401 = token 错 / 缺；403 = 命令不在白名单 / 源 IP 不在 CIDR；400 = 请求体不合法；500 = binary 不存在。

### 3.4 `/v1/exec/stream` 响应（SSE）

```
event: stdout
data: 22:14:50.123 IP 10.0.0.1.443 > 10.0.5.6.41252: Flags [S.]

event: stdout
data: 22:14:50.124 IP 10.0.0.1.443 > 10.0.5.6.41252: Flags [.]

event: stderr
data: tcpdump: listening on any, link-type LINUX_SLL (Linux cooked v1)

event: exit
data: {"exit_code":0,"duration_ms":30001,"timeout":false}
```

- 每行 stdout/stderr 一个 event。
- 客户端关连接 → agent 立刻 SIGKILL 子进程。
- 最后一个 event 必然是 `exit`（含 exit_code + duration + timeout 标志）。

### 3.5 鉴权

`Authorization: Bearer <token>`。Token 来源：

- **Swarm**：`docker secret create ai-ops-agent-token`，agent 启动时挂到 `/run/secrets/token` 读。
- **K8s**：`kubectl create secret generic ai-ops-agent-token --from-literal=token=...`，挂到 `/run/secrets/token`。
- **同一 token** 也填到平台 admin UI 的 host_agent connection 上。平台调 agent 时从 connection 里取。

### 3.6 命令白名单

`/etc/ai-ops-agent/allowed.yml` 在镜像里就内置；可读：[`agent/allowed.yml`](../agent/allowed.yml)。

默认放行的全是**取证型只读命令**：`ss / ip / iptables-save / nft / dmesg / lsof / ps / cat / tcpdump / dig / nslookup` 等。

**不放行**的（即便业务需要也不能直接走 agent）：

- 任何写操作（`iptables -A` / `ip route add` / `echo > /proc/sysrq-trigger`）—— 这些走平台的 `host_run_command` skill，强制 admin 审批 + 走 needs_confirmation 流。
- `curl / wget / nc` —— 防集群内 SSRF 和数据外泄。
- `nsenter / unshare` —— agent 自己会包一层 nsenter，禁止调用方手动塞防绕过。

白名单改动**必须重启 agent 容器**才能生效（启动时一次性加载）。

---

## 4. 部署

### 4.1 Build 镜像

```bash
# 在仓库根目录
docker build -t ai-ops/agent:1.0 -f Dockerfile.agent .

# 推到内网 harbor
docker tag ai-ops/agent:1.0 harbor.intra/ops/ai-ops-agent:1.0
docker push harbor.intra/ops/ai-ops-agent:1.0
```

镜像约 60-80 MB，alpine 基础 + iproute2 / iptables / tcpdump / nftables / py3-aiohttp。

### 4.2 Swarm 部署

```bash
# 1) 生成 token
TOKEN=$(openssl rand -hex 32)
echo $TOKEN | docker secret create ai-ops-agent-token -
echo "Token: $TOKEN  (粘到平台 admin UI 的 host_agent connection)"

# 2) 设环境变量
export AGENT_IMAGE=harbor.intra/ops/ai-ops-agent:1.0
export BACKEND_CIDR=10.20.0.0/16        # 可选：平台 backend 网段

# 3) 部署
docker stack deploy -c deploy/ai-ops-agent-swarm.yml ai-ops

# 4) 验证（在任一节点）
curl http://<node-ip>:9100/livez                                # → "ok"
curl -H "Authorization: Bearer $TOKEN" \
     http://<node-ip>:9100/v1/health | jq .
```

### 4.3 K8s 部署

```bash
# 1) 创建 namespace + token
kubectl create namespace ai-ops
TOKEN=$(openssl rand -hex 32)
kubectl -n ai-ops create secret generic ai-ops-agent-token \
    --from-literal=token=$TOKEN
echo "Token: $TOKEN"

# 2) 改 deploy/ai-ops-agent-k8s.yaml 里的 image: 字段指向你的 harbor

# 3) 部署
kubectl apply -f deploy/ai-ops-agent-k8s.yaml

# 4) 验证
NODE_IP=$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
curl http://$NODE_IP:9100/livez
curl -H "Authorization: Bearer $TOKEN" http://$NODE_IP:9100/v1/health | jq .
```

### 4.4 在平台后台创建 connection

1. 登录 admin UI（http://平台:8080） → 接入管理 → 新增
2. 类型选 `host_agent`，填：
   - **alias**：例如 `prod-cluster-1`
   - **kind**：`swarm` 或 `k8s`
   - **agent_port**：`9100`
   - **agent_token**：上面生成的 `$TOKEN`
   - （swarm）**docker_host**：`tcp://<manager-ip>:2375`，用于查节点 IP
   - （k8s）**kubeconfig**：粘集群 kubeconfig，用于查节点 IP
3. 点"验证连通"，应返回 ok。

---

## 5. 安全模型 / Threat Model

### 5.1 信任边界

```
            untrusted                    trusted
                                  │
   Internet / 内网用户  ───X──── │ ────  集群网络（agent listens here）
                                  │            │
                                  │       平台 backend
                                  │       （持有 token，知道节点 IP）
                                  │            │
                                  │       agent :9100
                                  │       （token + CIDR 双保险）
                                  │            │
                                  │       host root namespace
                                  │       （nsenter -t 1）
```

只有**平台 backend** 知道 token；token 在平台 admin UI 创建 connection 时一次性输入，存在 `platform_connection.config_json`（启用 `PLATFORM_ENCRYPTION_KEY` 后是密文）。

### 5.2 多层防御

| 层 | 控制 |
|---|---|
| 网络 | (a) agent 只绑节点 NIC（hostNetwork），不走 overlay；(b) `AGENT_ALLOWED_CIDR` 限源 IP；(c) 集群外建议 iptables 阻断 `:9100` 入站 |
| 协议 | Bearer token；HTTPS 由集群入口/反代负责（agent 自身只裸 HTTP，避免每节点维护证书） |
| 命令 | (a) 数组形式无 shell 注入；(b) 白名单只放取证型只读；(c) 写操作必须走平台 needs_confirmation |
| 进程 | timeout 强 SIGTERM→SIGKILL；输出截断防 OOM |
| 审计 | 双向日志：agent 侧记 `peer_ip + cmd + exit_code + duration`；平台 `platform_skill_call` 记完整调用链 |

### 5.3 已知风险 / 不解决的

- **节点被攻陷 → agent 自身可被滥用**：agent 是特权容器，节点 root 拿到了 = agent 已经被绕开了，不在 threat model 内。
- **平台 backend 被攻陷 → 持有 token 可调任意 agent**：依赖平台主体的 RBAC + 审计；后续可加 token rotation API。
- **HTTPS 没在 agent 内置**：私有化集群环境通常用 mTLS 或入口反代收口，不再让每节点维护证书。如需端到端 TLS，可加 sidecar Envoy。

---

## 6. 开发 / 调试

### 6.1 本地跑 agent

```bash
cd agent/
AGENT_AUTH_TOKEN=devtoken \
AGENT_ALLOWED_FILE=$(pwd)/allowed.yml \
python3 agent.py
# 然后另一个终端
curl -H "Authorization: Bearer devtoken" -H "Content-Type: application/json" \
     -d '{"cmd":["ss","-ltnp"],"nsenter":""}' \
     http://127.0.0.1:9100/v1/exec | jq .
```

注意 `nsenter:""` —— 本地 dev 不在 host ns，得显式跳过 nsenter wrap。

### 6.2 SSE 调试

```bash
curl -N -H "Authorization: Bearer devtoken" -H "Content-Type: application/json" \
     -d '{"cmd":["sh","-c","for i in 1 2 3; do echo line $i; sleep 1; done"],"nsenter":""}' \
     http://127.0.0.1:9100/v1/exec/stream
```

注意：`sh` 不在白名单里，所以这条 dev 例子会 403。要本地测，临时往 `agent/allowed.yml` 加 `- sh`。

### 6.3 K8s containerd 集群的 crictl 缺失

alpine 默认源没有 crictl 包。如要 `host_inspect_container_netns` 在 containerd 集群可用，两条路：

1. 在 `Dockerfile.agent` 末尾加一行从 GitHub releases 下 crictl 静态 binary（需要 build 期能联外网）。
2. K8s DaemonSet 加 initContainer 启动时拉 crictl 装到 emptyDir 共享卷。

---

## 7. Roadmap

| 项目 | 计划 |
|---|---|
| 端到端 TLS | sidecar Envoy 或 nginx，agent 镜像不动 |
| Token rotation | `POST /v1/admin/rotate_token` + 平台后台调 docker secret update |
| 抓包大文件 | 落对象存储/MinIO，agent 返回下载 URL（避免 SSE 把大 pcap 撑爆 backend）|
| 命令白名单热加载 | agent 加 inotify watch + 内存原子替换 |
| Prometheus metrics | `/v1/metrics` 暴露 exec_count / exec_latency_seconds / unauthorized_total |

---

## 8. 一句话

> SSH 是工具，不是必需品。HTTP agent 让"进宿主机查"变成一次有审计、有权限、有审批、可被 AI 协同调度的标准 HTTP 调用。
