# 部署 Setup · 让平台真的跑起来（不是 stub）

> 之前默认 `USE_STUB_*=true` 是错的——平台启动看起来"好了"但其实全是假数据。
> **现在默认全部走真实链路**，必须有 `.env` 才能正常工作。

---

## 1. 当前真实环境（截至 2026-05）

| 资源 | 地址 | 凭证 |
|---|---|---|
| **TiDB**（持久化） | `169.24.1.87:4000` | 库 `ai_alert` / 账密 `ai_alert` |
| **Zabbix**（监控） | `http://169.24.2.90:80/zabbix/api_jsonrpc.php` | 看 .env |
| 火山方舟 Code Plan | `https://ark.cn-beijing.volces.com/api/coding/v3` | 看 .env |

可达性测试（在跑 docker 的机器上）：

```bash
# TCP 通断
nc -vz 169.24.1.87 4000          # TiDB
curl -sS -o /dev/null -w "%{http_code}\n" \
  http://169.24.2.90:80/zabbix/api_jsonrpc.php   # 期望 405 / 401（说明端口通）

# Zabbix login（应返回 result token）
curl -sS -X POST http://169.24.2.90:80/zabbix/api_jsonrpc.php \
  -H "Content-Type: application/json-rpc" \
  -d '{"jsonrpc":"2.0","method":"user.login","params":{"username":"<u>","password":"<p>"},"id":1}'
```

---

## 2. 完整 `.env` 模板（直接贴）

把项目根的 `.env.example` 复制为 `.env`，按下面修：

```ini
# ============= 平台 =============
ADMIN_JWT_SECRET=<openssl rand -hex 32 生成强随机串>
ADMIN_BOOTSTRAP_USERNAME=admin
ADMIN_BOOTSTRAP_PASSWORD=<改成你自己强密码>

# 凭证加密；不设 → 凭证字段明文落库（明显告警），生产强烈建议设
# python scripts/generate_encryption_key.py 生成
PLATFORM_ENCRYPTION_KEY=<44 字符 Fernet key>

# ============= DB（TiDB）=============
STORE_BACKEND=sql
STORE_STRICT=true            # 推荐：DB 连不上就 fail，不静默走内存
DATABASE_URL=mysql+pymysql://ai_alert:ai_alert@169.24.1.87:4000/ai_alert?charset=utf8mb4

# ============= 模型（火山方舟 Code Plan）=============
AI_PROVIDER=volcengine_ark
AI_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
AI_API_KEY=<你的 Code Plan API Key>
AI_MODEL=ark-code-latest
AI_TIMEOUT_SECONDS=240
USE_STUB_AI=false

# ============= Zabbix =============
ZABBIX_BASE_URL=http://169.24.2.90:80/zabbix/api_jsonrpc.php
ZABBIX_USERNAME=<你的 zabbix 账号>
ZABBIX_PASSWORD=<你的 zabbix 密码>
ZABBIX_TIMEOUT_SECONDS=15
USE_STUB_ZABBIX=false

# ============= Swarm（如不用就留默认占位）=============
DOCKER_HOST=tcp://1.2.3.4:2375
DOCKER_BIN=docker

# ============= MCP server（不用 MCP 时留空）=============
MCP_API_KEYS=
MCP_ALLOW_ANON=false

# ============= Agent =============
AGENT_MAX_REASONING_STEPS=8

# ============= compose 端口（按需改）=============
BACKEND_PORT=5000
CHAT_PORT=8000
ADMIN_PORT=8080
```

---

## 3. 启动后必看的日志

平台启动时会"诚实地"告诉你**到底连上了什么**。看 `docker compose logs backend` 顶部：

✅ 正常生产配置应该看到的：
```
✅ 持久化层：SQLStore（DATABASE_URL=mysql+pymysql://ai_alert:***@169.24.1.87:4000/ai_alert）
✅ Zabbix 接入：http://169.24.2.90:80/zabbix/api_jsonrpc.php（账号 n14411）
✅ 默认模型：ark-code-latest（base_url=https://ark.cn-beijing.volces.com/api/coding/v3）
```

❌ 出现以下任何一条说明配置不对：
```
⚠️  数据库未连通，store 已降级到 InMemoryStore——所有 ... **重启即丢**
⚠️  USE_STUB_ZABBIX=true，Zabbix 返回的全是 mock 数据
⚠️  AI_API_KEY 未配置，模型调用必失败
⚠️  Zabbix 用户名/密码为空
```

---

## 4. 已存在 default-zabbix connection 用 stub 怎么办？

如果你之前用旧默认（stub=true）启动过，DB 里的 `default-zabbix` connection 记录会带 `use_stub=true`。**改 `.env` 不会自动覆盖已存在的 connection**——这是平台架构的特点（connection 是 admin 改的资产，不是 env 派生的）。

**两个修复办法**：

### A. admin 后台编辑（推荐）
1. 登录管理后台 → 接入管理
2. 编辑 `default-zabbix`
3. 关掉 "Stub 模式" 开关
4. 填上 ZABBIX_USERNAME / ZABBIX_PASSWORD
5. 点"验证连通"
6. 保存

### B. 直接清掉重新 bootstrap
```bash
docker compose exec backend python -c "
from runtime import create_runtime
from config import Config
rt = create_runtime(Config)
for c in rt.connection_manager.list(type_code='zabbix'):
    if c['name'] == 'default-zabbix':
        rt.connection_manager.delete(c['id'])
        print('deleted:', c['id'])
"
docker compose restart backend
```
重启时 ConnectionManager.ensure_bootstrap 会按当前 .env 重新建一份。

---

## 5. 24 小时数据怎么取的

skill `zabbix_get_host_overview` 现在接受 `lookback_hours` 参数：

| 用户问法 | 模型应该传 | 平台采样 |
|---|---|---|
| "现在状态" / "当前情况" | 不传或 1 | 5 分钟 × 12 点 = 1 小时 |
| "近 24 小时" / "今天的数据" | 24 | 1 小时 × 24 点 = 24 小时 |
| "近三天" / "最近 72h" | 72 | 2 小时 × 36 点 |
| "近一周" | 168 | 6 小时 × 28 点 |

平台始终保持采样点 12-30 之间，避免拉 24h 时给模型塞 288 个点。算法在 [`services/zabbix_client.py compute_window()`](../services/zabbix_client.py)。

返回里 `metric_summary.sample_window_minutes` 字段告诉模型实际拉了多长，模型应该在报告里讲清楚。

---

## 6. 一键自检脚本

跑这个脚本验证全部配置：

```bash
docker compose exec backend python -c "
from runtime import create_runtime
from config import Config
import json

rt = create_runtime(Config)

# DB
print('store:', type(rt.store).__name__)
print('hc:', json.dumps(rt.store.healthcheck(), ensure_ascii=False))

# Zabbix
zc = rt.connection_manager.list(type_code='zabbix')
for c in zc:
    print(f\"  zabbix conn: {c['name']} stub={c['config'].get('use_stub')}\")

# Skill 数
print('skills:', len(rt.skill_registry.list()))
print('runbooks:', rt.runbook_registry.list_keys())
"
```

正常输出应该有：
- store: SQLStore（不是 InMemoryStore）
- zabbix stub=False
- skills 30+
- runbooks 4 条
