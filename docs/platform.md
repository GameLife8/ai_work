# AI 运维平台 · 总览

> 面向运维场景的、可私有化部署的 Claude Code。模型默认走火山方舟，兼容
> 通义千问 / 智谱 GLM / DeepSeek / Moonshot 等任意 OpenAI 协议兼容的国产模型。

---

## 1. 架构一图

```
┌─────────────────┐   ┌─────────────────┐   ┌──────────────────┐
│  Chainlit Chat  │   │  Vue 管理后台   │   │  外部 MCP 客户端 │
│  (用户聊天)     │   │  (admin 配置)   │   │  Claude Code 等  │
└────────┬────────┘   └────────┬────────┘   └────────┬─────────┘
         │                     │                     │
         │   HTTP/WebSocket    │ /admin/api/v1/*     │ /mcp (HTTP)
         │                     │                     │
         ▼                     ▼                     ▼
┌────────────────────────────────────────────────────────────────┐
│                  ops_platform 内核 (kernel)                    │
│ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────┐  │
│ │ SkillReg-  │ │ Connection │ │  Model     │ │  Skill       │  │
│ │ istry      │ │  Manager   │ │  Manager   │ │  Invoker     │  │
│ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └──────┬───────┘  │
│       │              │              │               │          │
│  扫 skills/ 目录  按 type 路由   按 id 缓存    审计 + 写操作    │
│                                                  二次确认链    │
└──────────┬─────────────────────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────────────────────┐
│                          drivers                               │
│  zabbix · swarm · k8s · host_agent · http_api · jenkins ·      │
│  mcp_client · alert_analysis                                   │
└──────────┬─────────────────────────────────────────────────────┘
           │
           ▼
   远端真实资源（Zabbix API、Swarm manager、K8s API、agent DaemonSet）
```

平台四个核心维度：**接入(Connection) × 技能(Skill) × 模型(Model) × 用户/会话**。

---

## 2. 三个服务、三个容器

```yaml
# docker-compose.yml
backend:    Flask + Admin API           5000
chat:       Chainlit                    8000
admin-ui:   Vue static + nginx          80
```

启动方式：

```bash
cp .env.example .env       # 改 ADMIN_JWT_SECRET / AI_API_KEY / DATABASE_URL
docker compose build
docker compose up -d
```

详见仓库根目录 [`README.md`](../README.md)。

---

## 3. 维度一：Connection（接入）

每个 Connection 是某种 type 的具体连接配置（凭证 + 地址）。同类型可存多份。
当前内置 8 种 type：

| type | 说明 | 典型字段 |
|---|---|---|
| `zabbix` | Zabbix API | base_url / username / password |
| `swarm` | Docker Swarm 集群 | DOCKER_HOST / TLS 证书路径 |
| `k8s` | Kubernetes 集群 | kubeconfig / context / namespace |
| `host_agent` | 节点诊断 Agent（每节点 DaemonSet） | kind=k8s/swarm + 对应字段 |
| `http_api` | 通用 HTTP API（Jira / GitLab / CMDB…）→ 配 HTTP skill | base_url / 多种 auth_kind |
| `jenkins` | Jenkins CI/CD（实测兼容 2.190+）| base_url / username / api_token 或 password |
| `mcp_client` | 反向接第三方 MCP server，远端工具注册成本地 skill | server URL / auth |
| `alert_analysis` | 内置告警研判服务（虚拟） | — |

新增 Connection：admin 后台 **接入管理 → +新增接入**，按 type 选择驱动后表单字段会按 driver 的 schema 自动渲染。详见 [`ops_platform/drivers/`](../ops_platform/drivers/) 各 driver。

### 路由（同类型多个 Connection 怎么选）

调 skill 时按以下顺序解析具体走哪份 connection：

1. **Skill 参数显式 `connection_id`** —— 模型自己挑明
2. **会话级默认** —— Chainlit 右上角设置选的"当前 Swarm 集群 / Zabbix 实例"
3. **平台级默认** —— Connection 的 `is_default=true`

实现：[`ops_platform/context.py`](../ops_platform/context.py) 的 `SkillContext.connection_for()`。

---

### Signal 系统：跨域 pivot 从软约束变硬约束

每个 skill 在返回 `result` 时可附带 `_signals` 数组（[`ops_platform/signals.py`](../ops_platform/signals.py)），结构：

```python
{
  "type": "oom_kill",                   # 19 种内置类型
  "severity": "critical",
  "evidence": "任务 X 在节点 W1 退出码 137",
  "next_skill": "zabbix_get_host_overview",
  "next_args": {"host_query": "W1"},
  "context": {"node": "W1"},
}
```

agent 在 tool 循环里看到信号 → 自动渲染中文 hint 注入下一轮：

```
⚡ 上一步 skill 检测到结构化信号，建议如下跨域取证：
  1. 🚨 [oom_kill] 任务 X 在节点 W1 退出码 137（多半 OOMKilled）
     → 建议调用：zabbix_get_host_overview(host_query='W1')
```

模型路由到对应 skill 的概率显著上升。在 runbook 引擎里，signals 还能直接驱动条件分支
（`when: {type: has_signal, signal_type: oom_kill}`）。

**已发信号的 skill**：
`swarm_get_failed_tasks` / `swarm_check_service_health` / `swarm_get_service_logs_filter`
/ `k8s_describe_pod` / `k8s_list_pods` / `host_kernel_events` / `host_socket_overview`

### Tool-loop Digest：压缩 invoker 输出

`SkillInvoker.serialize_for_model()` 把每个 tool 返回压缩后才进 messages（原始数据全在 trace）：

- 大文本字段（logs / events / describe）头尾保留 60% / 40%，中间替换占位
- 列表只保留前 12 项
- 私有字段 `_xxx` 删除（**`_signals` 保留**——小但高价值）
- 整体 8KB 硬上限

**实测 5585 字节 envelope 压到 2631 字节，节省 53%**。模型上下文用得久，注意力不被刷屏。

---

## 4. 维度二：Skill（技能）

Skill 是平台对外暴露的"能力单元"，每个 skill 是 [`skills/`](../skills/) 目录下的一个子包：

```
skills/
└── swarm_check_service_health/
    └── __init__.py        # MANIFEST + run(ctx, **params)
```

启动时 [`ops_platform/loader.py`](../ops_platform/loader.py) 扫描整个目录，按 manifest 注册。**新增 skill 零代码改动**：建一个目录，里头是 manifest + 一个 `run` 函数。

### 当前 27 个 skill（按类别）

设计哲学是**少而精的"通用一把口"**——不给每种资源建独立 skill，而是 `kube_query` /
`swarm_query` / `host_query` 三把通用查询口用 `verb`/`category` 组合覆盖几十种 kubectl /
docker / shell 命令。这样模型不用记一堆 skill 名，工具集也小。

| 分类 | skill |
|---|---|
| **通用查询（三把口）** | `kube_query`（k8s get/describe/logs/top/events…，含 verb=logs）· `swarm_query`（docker service/node/task/stack…）· `host_query`（白名单 shell：ss/ip/df/ps/dmesg…）|
| swarm 写 | **swarm_scale_service** · **swarm_update_service_image** · **swarm_force_update_service** · **swarm_rollback_service** · **swarm_remove_service**(admin) |
| 集群巡检聚合 | `swarm_cluster_overview`（swarm：节点+服务+监控）· `k8s_cluster_overview`（k8s：节点+异常 pod+副本不匹配 deploy+监控）。配 `cluster_health_audit_swarm` / `cluster_health_audit_k8s` runbook，**agent 按用户点名的集群类型自动选对应 runbook**（说 codewave→k8s，说 sws-swarm→swarm）|
| k8s 写 | **k8s_scale_deployment** · **k8s_restart_deployment** · **k8s_rollout_undo** |
| 监控 | `zabbix_get_host_overview` · `zabbix_get_host_storage_overview` · `metric_query`（时序：mode=peak 找峰值 / mode=window 取时刻附近）|
| 主机执行 | **host_run_command**(admin) · **host_run_command_async**(admin，长命令 hybrid 同步/异步) · **host_capture_packets**(admin) · `host_check_task` · `host_list_tasks` · `host_list_nodes` |
| 网络/内核 | `host_inspect_container_netns`（进容器 netns 看 iptables）· `host_kernel_events`（dmesg + 老节点兼容）|
| CI/CD | `jenkins_query`（job/build/console/queue/node）|
| 告警 | `alerts_analyze_payload` |
| 编排入口 | `platform_get_runbooks` · `platform_run_runbook` |

加粗 = 写操作（read_only=False），走二次确认。`(admin)` = `visibility=admin`，仅 admin 可见可调。

> **历史合并**：`k8s_get_pod_logs` → 折进 `kube_query(verb=logs)`；`metric_query_peak` +
> `metric_query_window_around` → 合成 `metric_query(mode=…)`。冗余的薄包装一律收口到通用口。

### 工具加载：渐进披露（progressive disclosure）

27 个 skill 的 schema 全发给模型约 22K 字符。以前按关键词意图静态过滤，两个毛病：① 关键词脆，
还会**挡掉 signal 想调的 skill**；② 工具集随 query 变 → 破坏 prompt cache。现在换成
[`ops_agent/skill_domains.py`](../ops_agent/skill_domains.py) 的两层架构：

- **Layer 0 常驻（~7K，固定 → 缓存友好）**：三把通用查询口 + `zabbix_get_host_overview` +
  runbook 入口 + `load_skills` meta-tool。覆盖 ~80% 的问题。
- **Layer 1 按域懒加载**：模型要专科能力时调 `load_skills(domains=["swarm_write","monitoring"…])`，
  平台把那个域的 skill schema 加进工具集，**下一轮**即可调用。域有 `swarm_write` / `k8s_write` /
  `host_exec` / `monitoring` / `network_diag` / `cicd` / `alerts` 7 个。
- **signal 驱动自动加载**：scanner 发的 `next_skill` 若在某个域里，平台**自动**把该域加载进来——
  模型立刻能遵循 signal，不用先 load（彻底解决静态过滤"挡 signal"的硬伤）。

> 意图分类（`_classify_intent`）现在**只**用于注入"输出模式提示"（如 list_state 场景的 raw-first
> 展示），跟工具加载**完全解耦**。

### Skill manifest 字段

```python
MANIFEST = {
    "code":                       "swarm_force_update_service",  # 全局唯一
    "name":                       "强制更新 Swarm 服务",
    "description":                "...给模型看的何时使用 + 信号→下一步...",
    "category":                   "swarm",
    "required_connection_type":   "swarm",
    "read_only":                  False,
    "requires_admin_approval":    False,    # True 则只有 admin 能 confirm（user 调起 → admin 审）
    "visibility":                 "all",    # 'all' / 'admin'；admin-only 由 invoker.invoke 入口 RBAC 强制
    "confirmation_ttl_seconds":   300,
    "params_schema":              { ...JSON Schema... },
}

def run(ctx, *, service_name: str, connection_id: str | None = None) -> dict:
    return ctx.connection_for("swarm", connection_id).run(...)
```

> **质量门禁**：[`tests/test_skill_manifest_quality.py`](../tests/test_skill_manifest_quality.py) lint
> 每个 manifest——描述长度、长描述必带示例、易混淆 skill 必须互指。新 skill 自动强制走规范。

### Skill manifest 字段

```python
MANIFEST = {
    "code":                       "swarm_force_update_service",  # 全局唯一
    "name":                       "强制更新 Swarm 服务",
    "description":                "...给模型看的何时使用 + 信号→下一步...",
    "category":                   "swarm",
    "required_connection_type":   "swarm",
    "read_only":                  False,
    "requires_admin_approval":    False,    # True 则只有 admin 能 confirm
    "visibility":                 "all",    # 'all' / 'admin'
    "confirmation_ttl_seconds":   300,
    "params_schema":              { ...JSON Schema... },
}

def run(ctx, *, service_name: str, connection_id: str | None = None) -> dict:
    return ctx.connection_for("swarm", connection_id).run(...)
```

### 写操作二次确认

`read_only=False` 的 skill 调用流程：

```
模型 → invoker.invoke()
    → 入口 RBAC：visibility=admin 的 skill，非 admin 直接 forbidden
    → 因为 read_only=False，不立即执行
    → 落 pending_action 表（记 session_id），返回 needs_confirmation + token
模型 → 出"提议+风险"中文说明，停止 tool 循环
chainlit / 后台 → 渲染 ✅/❌ 卡片
用户点 ✅
    → invoker.confirm(token, ctx)
    → 校验状态 / TTL / 会话绑定 / 权限 → 真正执行 → pending 改 executed
模型 → follow_up_after_action() 给最终中文总结
```

**安全要点**（[`ops_platform/invoker.py`](../ops_platform/invoker.py)）：
- **会话绑定**：token 记录创建时的 `session_id`。**非 admin** 必须从同一会话确认——
  防止 token 泄露后被攻击者在别的会话重放。**admin 可跨会话确认**（后台审批是 admin 职责，
  后台 session ≠ 原聊天 session）。
- **审计脱敏**：skill_call 审计落库前，用户参数里的密码 / api_key / `-p<pw>` / `KEY=VALUE`
  等凭证会脱敏成 `***`（`_redact_audit_args`）。但**平台生成的 `_extra`（如 confirmation_token）
  不脱敏**——审计页靠它反查发起人。
- **入口 RBAC**：`visibility=admin` 的 skill 在 `invoker.invoke()` 执行层拦截，不只靠 LLM
  tool schema 过滤（HTTP API / runbook 引用 / 内部调用都挡得住）。

### Runbook（诊断剧本）· 图执行

> 详细见独立文档 [`docs/runbook.md`](runbook.md)。

剧本现在有**两种形态**，并存使用：

| 形态 | 文件 | 用途 |
|---|---|---|
| **图执行**（推荐） | DB 表 `platform_runbook` + [`runbook_engine.py`](../ops_platform/runbook_engine.py) | 平台**自动执行** DAG，模型只写最终报告 |
| 文档型（legacy） | [`runbooks.py`](../ops_platform/runbooks.py) Python dict | 给模型读的"思路指引"，模型自己挑 skill |

**图执行**通过 [`platform_run_runbook`](../skills/platform_run_runbook/__init__.py) 这个 skill 触发；
模型在 prompt 引导下面对复合问题会主动调用它。引擎能力：

- DAG 调度（环检测、visited 防重入、deadline 兜底）
- 三种引用语法（`$user.X` / `$nodes.id.path` / `$signals.type.field`）
- 9 种条件类型（has_signal / status_ok / field_eq / any_of / not 等可嵌套）
- 节点级 timeout / retry / on_error（fail/skip/continue）
- 写 skill 入图自动拒绝（保护 needs_confirmation 链路）
- admin 后台 YAML 编辑 + 校验 + **热加载**，无需重启
- 每次执行完整落库 [`platform_runbook_execution`](../ops_platform/store.py)，admin UI 可逐节点回放

**文档型 runbook** 通过 `platform_get_runbooks` skill 让模型按需查阅：

| 剧本 | 触发 |
|---|---|
| `swarm_service_not_starting` | 服务起不来/持续重启 |
| `swarm_service_slow_or_high_latency` | 服务慢/超时 |
| `host_resource_alert` | 主机 CPU/MEM/磁盘告警 |
| `k8s_pod_crashloop` | CrashLoopBackOff/OOMKilled/Evicted |
| `k8s_deploy_rollout_failed` | rollout 卡住 |
| `alert_payload_triage` | 原始告警 payload |
| **`network_troubleshooting`** | **网络不通/丢包/iptables（用 host_agent）** |
| **`node_health_audit`** | **节点深度体检** |

新增剧本只要往 `RUNBOOKS` dict 里加一项，无需改 prompt 或 loader。

---

## 5. 维度三：Model（模型）

兼容任何走 OpenAI 协议的模型。后台 **模型管理 → +新增模型** 填四个字段：

```
provider:  volcengine_ark / qwen / zhipu / deepseek / moonshot / openai_compatible
base_url:  https://ark.cn-beijing.volces.com/api/v3
api_key:   你的 key
model:     ep-xxx / qwen-plus / glm-4 / deepseek-chat / moonshot-v1-32k / ...
```

当前的 agent 默认拿 `is_default=true` 那条；future：会话级允许切换模型。

---

## 6. 维度四：用户（User）

两个角色：`admin` / `user`。

- **admin** 看到所有页面 + 所有 skill；可确认 `requires_admin_approval=True` 的写操作
- **user** 只能登 Chainlit 聊天；模型的 tool 列表里不会出现 `visibility=admin` 的 skill

登录方式：账号密码 + JWT。Chainlit / Admin API 共用同一份 `platform_user` 表，详见
[`ops_platform/auth.py`](../ops_platform/auth.py) 和 [`admin_app/blueprint.py`](../admin_app/blueprint.py)。

默认管理员：`admin` / `admin123`，**生产部署务必通过 `ADMIN_BOOTSTRAP_PASSWORD` 改掉**。

---

## 7. 三个执行入口共享一个 invoker

| 入口 | 协议 | 文件 |
|---|---|---|
| Chainlit 聊天 | WebSocket | [`chainlit_app.py`](../chainlit_app.py) |
| Admin REST API | HTTP JSON | [`admin_app/blueprint.py`](../admin_app/blueprint.py) `/admin/api/v1/skills/<code>/invoke` |
| MCP server | streamable HTTP | [`mcp_app.py`](../mcp_app.py) + [`ops_platform/mcp_server.py`](../ops_platform/mcp_server.py) |

三者最终都调 `runtime.skill_invoker.invoke(code, params, ctx)`。这意味着：
- 写操作的二次确认链对三者一视同仁
- 审计落库的 schema 一致（[`platform_skill_call` 表](../ops_platform/store.py)）
- 增减 skill 后所有入口同时生效

---

## 8. 数据持久化

DB 默认 TiDB（MySQL 兼容）；本地调试可设 `STORE_BACKEND=memory`，或用 `docker-compose.yml`
里托管的本地 MySQL（`db` 服务）。表清单（截至本版本）：

### 平台表
存储层拆成三个文件（原 1775 行单文件拆开，零迁移成本）：
[`store.py`](../ops_platform/store.py)（入口 + `attach_platform_store` + re-export）·
[`store_memory.py`](../ops_platform/store_memory.py)（内存版）·
[`store_sql.py`](../ops_platform/store_sql.py)（SQLAlchemy 版）。

- `platform_user` — 用户 + 密码 hash（PBKDF2-SHA256）+ 角色
- `platform_connection` — 接入的配置；``config_json`` **Fernet 对称加密**，前缀 ``enc:v1:``
- `platform_model_config` — 模型配置；``api_key`` **Fernet 加密** + `tool_choice_preference`（auto/required/none，admin 按模型实测配）
- `platform_skill_call` — 所有 skill 调用审计（敏感参数落库前脱敏）
- `platform_pending_action` — 写操作待确认队列（记 session_id 做会话绑定）
- `platform_prompt_segment` — 5 段 system prompt（admin 后台可编辑、热加载）
- `platform_runbook` — 图执行剧本定义
- `platform_runbook_execution` — 每次 runbook 执行的完整轨迹（节点状态 + 信号 + 报告）
- `platform_async_task` — 异步长命令任务（host_run_command_async）

### 并发安全：bootstrap 双重检查锁
首启空库时，多 chainlit worker / 多容器并发跑 `ensure_bootstrap` 会各自 check-then-act 都看到空 →
各建一遍 env 种子接入 → 重复。修复用**双重检查锁**：快路径无锁查 `list()`；空了拿
`store.bootstrap_lock()`（MySQL `GET_LOCK` 跨进程命名锁）→ 锁内**再查一次** → 没有才建。
见 [`connection_manager.py`](../ops_platform/connection_manager.py) / [`model_manager.py`](../ops_platform/model_manager.py)。

### 模型配置变更总线
`ModelManager` 是模型配置 mutation 的唯一入口：create/update/delete 自动失效 `OpsModelClient`
缓存 + 触发 `on_change` 回调（alert pipeline 的 legacy client 一并刷新）。admin 改完默认模型
**无需重启**即下一次会话生效。

### 优雅停机
`runtime.shutdown()` 停 async-task poller + 关 host_agent 连接池 + dispose DB engine；
`app.py` 注册 atexit + SIGTERM/SIGINT。k8s rolling update / docker stop 时正在跑的任务有限期收尾。

### 凭证加密（[`ops_platform/crypto.py`](../ops_platform/crypto.py)）

设置 ``PLATFORM_ENCRYPTION_KEY`` 启用：

```bash
# 生成
python scripts/generate_encryption_key.py
# 输出 44 字节 base64 串，写到 .env

# 启动后第一次 init 会自动把所有现有明文 row 重新加密一遍（幂等，多跑无害）
docker compose up -d backend
```

**特性**：
- 前缀 ``enc:v1:`` 标识；旧明文 row 仍能读，**渐进式迁移零停机**
- key 支持 44 字节 Fernet key 或任意 passphrase（自动 sha256 派生）
- 未配 key 时降级为明文 + warning 日志（开发环境友好）
- 解密失败（错 key / 数据损坏）记 ERROR 但不抛异常，返回原值，避免单条坏数据让整个连接列表崩

**⚠️ 丢 key 等于丢数据**——务必跟 DB 备份分开存放（Vault / KMS / 1Password）。

### 业务表（[`models/db.py`](../models/db.py)，告警链路用）
- `alert_event` / `alert_decision` / `incident` / `incident_alert_rel`
- `chat_session` / `chat_message`

### env 是种子，DB 是真相（single source of truth）

> 一句话：`.env` 里的 `ZABBIX_*` / `AI_*` / `DOCKER_*` 只在第一次启动时被
> `ensure_bootstrap` 写进 `platform_connection` / `platform_model_config` 表，
> 之后**所有运行期决策都读 DB**。admin 在后台改完连接立即生效，无需重启进程。

#### 启动时序（[`runtime.py`](../runtime.py) `create_runtime`）

```
1. 建 connection_manager / model_manager（持有 store）
2. ensure_bootstrap(config_cls)：
     - 第一次启动：把 env 里 ZABBIX_* / DOCKER_* 等读出来，落 DB（带 is_default=true）
     - 之后启动：DB 里已有 row → 跳过，env 改了也不会覆盖 DB
3. _build_legacy_clients(runtime, config_cls)：
     - 从 DB 默认 connection 派生 zabbix_client / docker_swarm_client
     - 从 DB 默认 model 派生 ai_client（alert pipeline 用的）
     - context_fetcher / alert_service / alert_analysis_service 也跟着重建
4. http_skill_loader.reload() / runbook_registry.reload()：从 DB 加载所有 YAML
5. _attach_refresh_method：挂 runtime.refresh_legacy_clients()
6. _kick_off_async_health_check：后台线程把每条 connection 拨号一次，状态写回 DB
```

**关键：第 3 步在第 2 步之后**——alert pipeline 老链路 (`POST /api/v1/alerts/*`) 看到的
`zabbix_client.username` 跟 admin 后台 `connection_manager.get_default("zabbix")` 是同一份。

#### 改了 connection 之后的传播路径

```
admin UI 改默认 zabbix connection
   ↓
PATCH /admin/api/v1/connections/<id>
   ↓
connection_manager.update(...)             # 写 DB（Fernet 加密）
   ↓
检测到 is_default=True → 自动调用
runtime.refresh_legacy_clients()
   ↓
_build_legacy_clients(...)
   ↓
runtime.zabbix_client / .ai_client / .docker_swarm_client 全部换成新实例
runtime.context_fetcher / .alert_service 也是新的
   ↓
下一次告警分析请求立即用新凭证
```

模型 (`platform_model_config`) 走同一条路径，PATCH `/admin/api/v1/models/<id>` 触发
`refresh_legacy_clients`。

#### 为什么 env 还留着

- **首次部署的种子**：新装空 DB，需要一份"出厂默认"才能跑起来
- **本地调试 fallback**：`STORE_BACKEND=memory` 没 DB 时，env 直接当配置用
- **不可恢复降级**：DB 默认连接被误删时（极端场景），代码里有 env fallback 兜底

但只要 DB 里有 row，**env 不再被读**——这是排查"我后台明明改了为什么没生效"问题的钥匙。

#### 自检命令

启动日志里会打印当前真正生效的来源（[`runtime._log_data_source_state`](../runtime.py)）：

```
✓ 默认 zabbix connection: dmz-cluster01 → http://169.24.2.90/zabbix/api_jsonrpc.php (use_stub=False)
✓ 默认 model: ark-code-latest → https://ark.cn-beijing.volces.com/api/coding/v3 (api_key=ed7c***ab12)
```

如果看到 `use_stub=True` 或 `api_key 未配置`，去 admin UI **接入管理 / 模型管理** 改 row，
不要去改 .env——env 现在是只读种子。

---

## 9. 部署

仓库根目录已就绪：

```
ai_work/
├── Dockerfile.backend           # python:3.11-slim + docker CLI + kubectl + gunicorn
├── Dockerfile.chat              # 同 base，跑 chainlit
├── Dockerfile.admin-ui          # multi-stage: node 构建 + nginx
├── admin_ui/nginx.conf          # SPA + /admin/api 反代
├── docker-compose.yml           # 三服务编排
├── .env.example                 # 完整环境变量模板
└── deploy/                      # host_agent 部署清单
    ├── ai-ops-agent-k8s.yaml
    └── ai-ops-agent-swarm.yml
```

详见 [README.md](../README.md) 启动章节。

---

## 10. 文档导航

| 文档 | 内容 |
|---|---|
| [`README.md`](../README.md) | 项目根 README，启动指引 |
| **`platform.md` (本文)** | 平台总览 |
| [`runbook.md`](runbook.md) | 诊断剧本图执行引擎详解（DSL / 条件 / 信号 / 调试） |
| [`http-skill.md`](http-skill.md) | YAML 声明式接入外部系统（Tier 1 + Tier 2/3 路线图） |
| [`host-agent.md`](host-agent.md) | host_agent 部署 + host_* skill 详解 |

---

## 11. 路线图

| 状态 | 项 |
|---|---|
| ✅ | 平台 kernel · skill 插件机制 · 8 driver · 28 skill · 6 graph runbook |
| ✅ | 三入口（Chainlit / Admin / MCP） |
| ✅ | 写操作二次确认链 + 入口 RBAC + 会话绑定（admin 可跨会话审批）|
| ✅ | 火山方舟（Code Plan）+ 国产模型兼容 + 模型级 tool_choice 偏好 |
| ✅ | host_agent 替代 SSH（K8s + Swarm，containerd/docker 双适配） |
| ✅ | Connection 凭证 Fernet 加密 + 自动迁移 + `STRICT_ENCRYPTION` 生产强制 |
| ✅ | **结构化 Signals + agent 自动 hint 注入**（跨域 pivot 从软变硬） |
| ✅ | **渐进披露工具加载**（core 常驻 + load_skills 按域懒加载 + signal 自动加载）|
| ✅ | **Tool-loop digest**（自动压缩节省 token） · **token usage 监控** |
| ✅ | **DB-backed prompt 段落库 + admin UI 编辑**（5 段 + CodeMirror） |
| ✅ | **诊断剧本图执行引擎**（DAG + DSL + 信号驱动 + admin 编辑 + 执行回放） |
| ✅ | **HTTP Skill (Tier 1)** — YAML 声明式接入外部系统，热加载，详见 [docs/http-skill.md](http-skill.md) |
| ✅ | **Jenkins CI/CD 接入** — jenkins driver + jenkins_query skill |
| ✅ | **审计脱敏**（凭证不落明文）· **bootstrap 并发安全**（双重检查锁）· **优雅停机** |
| 🚧 | HTTP Skill **Tier 2**：OpenAPI/Swagger 批量导入生成 YAML 草稿 |
| 🚧 | HTTP Skill **Tier 3**：Remote MCP Connection（直接 wrap 外部 MCP server）|
| 🚧 | 自定义 HTTP node-agent（替代每节点暴露 dockerd TCP）|
| 🚧 | 告警自动诊断（alert → 自动选 runbook → 推送给值班） |
| 📅 | 会话级模型切换 |
| 📅 | Plugin marketplace（外部 git 安装 skill） |
