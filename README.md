# AI 运维平台

> 面向运维场景的、可私有化部署的 Claude Code。模型默认走火山方舟，
> 兼容通义千问 / 智谱 GLM / DeepSeek / Moonshot 等任意 OpenAI 协议兼容的国产模型。

---

## 一句话说明

把"运维老司机"的查问题套路、动作执行能力、跨集群多接入、二次确认审批、统一审计，
全部沉淀到一个平台里。运维同学和 AI 共用同一份 skill，AI 给思路、人给确认。

---

## 它能干什么

| 类型 | 例子 |
|---|---|
| 🔍 查信息 | "WYY-DB09 主机所有硬盘当前情况" |
| 🩺 排障诊断 | "iiot-haitu_seatable 这个服务为什么起不来" — 跨容器 + 主机层联动 |
| ☸️ K8s 巡检 | "dmz-cluster01 上 default 命名空间所有 pod 状态" |
| 🌐 网络排障 | "worker-3 节点上 80 端口没人监听，帮我查一下" — 走 host_agent + nsenter，**不需要 SSH** |
| 📥 告警研判 | 直接粘贴一段 Zabbix payload JSON |
| ⚡ 执行操作 | "重启 my-app 这个 deployment" — 自动走二次确认 |

## 三个服务、三个容器

| 服务 | 端口 | 干啥 |
|---|---|---|
| `backend` | 5000 | Flask + Admin API + 告警接入 |
| `chat` | 8000 | Chainlit 用户聊天 |
| `admin-ui` | 8080 | Vue 静态后台 + nginx，反代到 backend |

## 快速启动（在 WSL 或 Linux）

```bash
# 1. 装 docker compose 插件（第一次）
sudo apt-get update && sudo apt-get install -y docker-compose-plugin

# 2. 准备 .env
cp .env.example .env
# 编辑：
#   ADMIN_JWT_SECRET    强随机串
#   ADMIN_BOOTSTRAP_PASSWORD  生产改密
#   AI_API_KEY / AI_BASE_URL / AI_MODEL  火山方舟实际值
#   DATABASE_URL        指向 TiDB；调试可设 STORE_BACKEND=memory

# 3. 起来
docker compose build
docker compose up -d

# 4. 看日志
docker compose logs -f backend chat admin-ui
```

访问：

| 入口 | URL | 默认账号 |
|---|---|---|
| 管理后台 | <http://localhost:8080> | admin / admin123 |
| 聊天页 | <http://localhost:8000> | 同上 |
| 后端 API（健康检查） | <http://localhost:5000/health> | — |

## 几个关键概念

| 维度 | 说明 |
|---|---|
| **Connection** | 接入。Zabbix / Swarm / K8s / host_agent 各一个 driver；同类型可存多份；`config_json` **Fernet 加密落库** |
| **Skill** | 能力。`skills/<name>/__init__.py` 一个目录一个能力，**新增零代码改动** |
| **Signals** | skill 主动发出结构化信号（OOM / 磁盘满 / 镜像拉不下来…），agent 自动渲染中文 hint 注入下一轮——**跨域 pivot 从软约束变硬约束** |
| **Runbook（图执行）** | 平台**自动按 DAG 跑一连串 skill**，模型只写最终中文五段式报告。admin 后台 YAML 编辑 + 热加载。详见 [docs/runbook.md](docs/runbook.md) |
| **写操作二次确认** | `read_only=False` 的 skill 走 pending_action 队列，先弹卡片再执行；admin-only 写 skill 走 visibility + approval 三重锁 |
| **Prompt 段落库** | system prompt 拆 5 段落 DB，admin 后台 CodeMirror 编辑；**改完即时生效不重启** |
| **Model** | 火山方舟（Code Plan）/ 通义 / 智谱 / DeepSeek 等任意 OpenAI 兼容模型 |
| **host_agent** | 每节点一个特权 DaemonSet，**用 nsenter 替代 SSH 排障** |
| **MCP server** | 平台 skill 暴露成 MCP，Claude Code / Cursor 可直接挂载使用 |

详见 [`docs/platform.md`](docs/platform.md)。

## 接外部 AI Agent（MCP）

平台暴露一个 MCP server（streamable HTTP）：

```
URL:  http://your-host:8765/mcp
Auth: Authorization: Bearer <MCP_API_KEYS 之一>
```

Claude Code / Cursor 等 MCP 客户端配置后，可以直接调用平台的 27 个 skill 完成排障。详见
[`docs/platform.md`](docs/platform.md) MCP 章节。

## 部署 host_agent（替代 SSH）

```bash
# K8s
kubectl apply -f deploy/ai-ops-agent-k8s.yaml

# 或 Swarm
docker stack deploy -c deploy/ai-ops-agent-swarm.yml ai-ops
```

然后到 admin 后台 → 接入管理 → 新增"节点诊断 Agent"接入。详见
[`docs/host-agent.md`](docs/host-agent.md)。

## 文档目录

```
docs/
├── platform.md                       # 平台总览（推荐先读）
├── runbook.md                        # 诊断剧本图执行引擎详解
└── host-agent.md                     # 替代 SSH 的节点排障方案
```

## 目录结构

```
ai_work/
├── app.py                       # Flask 入口（被 backend 容器跑）
├── chainlit_app.py              # Chainlit 入口（被 chat 容器跑）
├── mcp_app.py                   # MCP server ASGI 入口（独立可选）
├── runtime.py                   # 装配 connection_manager / model_manager / skill_registry / invoker
├── config.py                    # 环境变量统一聚合
├── ops_platform/                # 平台 kernel
│   ├── registry.py              # SkillRegistry
│   ├── loader.py                # 扫描 skills/ 目录
│   ├── invoker.py               # 统一执行 + 二次确认链 + tool-loop digest
│   ├── signals.py               # 结构化 signals 定义 + agent 注入 hint
│   ├── runbook_engine.py        # 图执行引擎（DAG + DSL + executor）
│   ├── runbook_seeds.py         # 默认出厂剧本（图形式）
│   ├── connection_manager.py    # CRUD + client 缓存
│   ├── model_manager.py
│   ├── context.py               # SkillContext (connection_for 路由)
│   ├── auth.py                  # 简版 JWT + PBKDF2
│   ├── crypto.py                # Fernet 凭证加密
│   ├── prompts.py               # 5 段 system prompt 默认值
│   ├── runbooks.py              # legacy 文档型剧本（给模型读）
│   ├── store.py                 # 平台表 + ORM 简版
│   ├── mcp_server.py            # MCP tool 暴露
│   └── drivers/                 # 接入驱动
│       ├── zabbix.py · swarm.py · k8s.py · host_agent.py · alert_analysis.py
├── skills/                      # 27 个 skill 插件（每目录一个）
├── services/                    # 底层 client（zabbix / swarm / k8s / host_agent）
├── admin_app/                   # Flask 后台蓝图（/admin/api/v1/*）
├── admin_ui/                    # Vue 3 + Vite 后台（→ Dockerfile.admin-ui 出 nginx 镜像）
├── routers/                     # 老的告警接入 router
├── models/                      # 告警 / incident 业务表 ORM
├── deploy/                      # host_agent K8s/Swarm 部署清单
├── docs/                        # 文档
├── Dockerfile.backend
├── Dockerfile.chat
├── Dockerfile.admin-ui
├── docker-compose.yml
└── .env.example
```

## License

本项目采用 **GNU AGPL-3.0** 协议开源（与 [Logseq](https://github.com/logseq/logseq) 同款）。

完整协议见根目录 [`LICENSE`](LICENSE)。

要点：

- ✅ 自由使用、修改、私有化部署、商用、二次分发
- ⚠️ **网络使用条款（AGPL 第 13 条）**：如果你修改了源码并以网络服务形式
  对外提供（哪怕只是对公司内部用户提供），必须把修改后的完整源码
  在合理途径下提供给所有用户
- ⚠️ 衍生作品必须采用同样的 AGPL-3.0 协议（强 copyleft）
- ⚠️ 必须保留版权与协议声明

简单说：**自己用 / 公司内部部署完全自由**；**改动后通过网络对外服务的话，
源码须开放回馈社区**。这是为了让平台演进的成果能持续反哺生态。

如对你的使用场景有疑问，建议先咨询法务或参考 [GNU AGPL-3.0 FAQ](https://www.gnu.org/licenses/gpl-faq.html)。

---

## 当前状态

✅ 已完成：
- 平台 kernel（skill 插件机制 / 8 driver / 27 skill / 5 graph runbook）
- 三入口（Chainlit + Admin + MCP）共用 invoker
- 写操作二次确认链 + 三重锁（visibility + needs_confirmation + admin approval）
- 国产模型兼容（火山方舟 Code Plan 为默认 + 通义/智谱/DeepSeek）
- host_agent 替代 SSH 排障（K8s + Swarm，含 containerd 适配）
- Connection 凭证 Fernet 加密 + 自动迁移
- 结构化 Signals 系统（19 种内置类型，agent 自动注入跨域 pivot hint）
- Tool-loop digest（每次 skill 返回自动压缩，节省 ~50% token）
- DB-backed prompt 段落库（admin 后台 CodeMirror 编辑、热加载）
- **诊断剧本图执行引擎**（DAG + 引用 DSL + 9 种条件 + 信号驱动 + 执行回放）
- Vue 后台 UI 清新简约风格 + CodeMirror 编辑器
- Docker 化（三个 Dockerfile + compose）

🚧 路线图：详见 [`docs/platform.md` § 路线图](docs/platform.md#11-路线图)。
