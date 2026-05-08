# HTTP Skill · 数据驱动接入外部系统

> 用 YAML 声明一个 HTTP API 端点 → 自动变成平台 skill。
> 不写 Python 代码、不重启服务、admin 后台改完热加载。

代码：[`ops_platform/http_skill.py`](../ops_platform/http_skill.py) ·
[`ops_platform/http_skill_loader.py`](../ops_platform/http_skill_loader.py) ·
[`services/http_api_client.py`](../services/http_api_client.py) ·
[`ops_platform/drivers/http_api.py`](../ops_platform/drivers/http_api.py)

---

## 1. 解决什么问题

随着平台落地，要对接的外部系统越来越多——Jira、GitLab、Argo CD、Grafana、内部 CMDB、
工单系统、配置中心……每个系统平均 5-10 个 endpoint。

如果每个 endpoint 都写一个 Python skill：
- 50-100 个 skill 文件，维护爆炸
- 每个新增都要改代码 / push commit / 重 build 镜像 / 走 CI
- 非工程师 admin 不能直接配

**HTTP Skill** 把这件事变成数据：

```
admin 后台写一份 YAML
   ↓
落到 platform_http_skill 表
   ↓
HttpSkillLoader.reload() 注册到 SkillRegistry
   ↓
和原生 Python skill 共用同一份执行链路、审计、二次确认、信号、MCP、runbook
```

---

## 2. 整体架构

```
   admin 后台 (HttpSkills.vue + CodeMirror YAML 编辑)
        │
        │  PUT /admin/api/v1/http-skills/<code>
        ▼
   admin_app/blueprint.py — 校验 + 落库
        │
        ▼
   platform_http_skill 表
        │
        │  http_skill_loader.reload()
        ▼
   SkillRegistry (source='http' 标记)
        │
        ├─ openai_tools()  → 模型看到这个 skill
        ├─ invoker.invoke() → HttpSkillRunner.run(spec, params, ctx)
        │                       │
        │                       ├ resolve connection (http_api type)
        │                       ├ Jinja2 沙箱渲染 path/query/headers/body
        │                       ├ HttpApiClient.request()
        │                       ├ apply response_extract
        │                       └ evaluate signal_rules
        ├─ MCP server 暴露给 Claude Code / Cursor
        ├─ runbook 引擎可引用
        └─ 写操作 needs_confirmation 链路自动适配
```

**关键设计**：HTTP skill 在 SkillRegistry 里就是普通 SkillSpec，只是 `source='http'`、
`handler` 是包装了 spec 的闭包。所有上层逻辑感知不到差异。

---

## 3. 完整 YAML 字段速查

```yaml
# ── 标识 ──
code: jira_search_issues          # 全局唯一；要求小写字母+数字+下划线
name: 在 Jira 里搜 issue
description: |
  给模型看的"何时使用 + 信号→下一步"。
  写得越具体，模型越能精准选择。
category: ticketing               # 分类标签

# ── 与 connection 的绑定 ──
connection_id: jira-prod          # 关联到一个 type=http_api 的 connection
                                   # 留空 → 调用时由模型/默认 http_api connection 决定

# ── 平台元属性（与原生 Python skill 完全一致） ──
read_only: true
requires_admin_approval: false    # write skill 强烈建议设为 true
visibility: all                   # all | admin
confirmation_ttl_seconds: 300
enabled: true                     # false → 不注册到 SkillRegistry

# ── HTTP 调用 ──
method: POST                      # GET | POST | PUT | PATCH | DELETE | HEAD | OPTIONS
path: /rest/api/3/search          # 相对 connection.base_url；可含 {{ ... }} 模板
                                   # 也可以写 https://full.url/... 强制完整 URL

# 可选：query string，每个值经 Jinja2 渲染
query_template:
  page: "{{ page | default(1) }}"
  expand: "renderedFields"

# 可选：调用级 header（与 connection.custom_headers 合并）
headers_template:
  X-Request-ID: "{{ uuid() }}"
  X-Trace-Source: "ai-ops-platform"

# 可选：仅 POST/PUT/PATCH 用；Jinja2 渲染后必须是合法 JSON
body_template: |
  {
    "jql": "{{ jql }}",
    "maxResults": {{ limit | default(20) }},
    "fields": ["summary", "status", "assignee", "updated"]
  }

# ── 模型工具签名 ──
params_schema:                    # 标准 JSON Schema
  type: object
  required: [jql]
  properties:
    jql:
      type: string
      description: "Jira Query Language 表达式，如 'project=OPS AND status=Open'"
    limit:
      type: integer
      default: 20
      maximum: 100

# ── 响应精简 ──
response_extract:                 # JSONPath 取值；最常用 [*] 数组展开
  total: '$.total'
  issue_keys: '$.issues[*].key'
  first_summary: '$.issues[0].fields.summary'

# ── 结构化信号 ──
signal_rules:                     # 把 HTTP 响应转成跨域 pivot 信号
  - when: { type: http_status, eq: 401 }
    emit:
      type: auth_failure
      severity: critical
      evidence: "Jira 401，凭证可能失效"
  - when: { type: http_status, gte: 500 }
    emit:
      type: backend_error
      severity: critical
      evidence: "Jira 5xx：{{ status }}"
  - when: { type: response_field, path: $.total, gt: 100 }
    emit:
      type: too_many_results
      severity: warning
      evidence: "结果超 100 条（实际 {{ response.total }}），建议收窄 jql"

# ── 网络 ──
timeout_seconds: 30
retry: 0
```

---

## 4. 引用语法

### 4.1 Jinja2 模板（path / query / headers / body）

平台用 **Jinja2 SandboxedEnvironment**（防 RCE）。可用：

| 语法 | 例子 |
|---|---|
| 变量 | `{{ jql }}` 或 `{{ params.jql }}`（两种都支持）|
| 默认值 | `{{ limit \| default(20) }}` |
| JSON 序列化 | `"items": {{ items \| tojson }}` |
| URL 编码 | `/users/{{ name \| urlencode }}` |
| 当前时间 | `{{ now() }}`（ISO UTC）|
| UUID | `{{ uuid() }}`（hex）|
| Unix 时间戳 | `{{ timestamp() }}` |

### 4.2 Response Extract DSL

| 语法 | 含义 |
|---|---|
| `$.foo` | 取顶层字段 |
| `$.foo.bar.baz` | 嵌套 |
| `$.list[0]` | 数组下标 |
| `$.list[*]` | 数组所有元素（返回 list）|
| `$.list[*].name` | 数组每个元素的子字段（返回 list of names） |
| `$.list[-1]` | 不支持负索引（写具体下标）|

### 4.3 Signal Rules `when` 类型

| `type` | 字段 | 含义 |
|---|---|---|
| `http_status` | `eq` / `ne` / `gte` / `lte` / `in: [...]` | HTTP 状态码 |
| `response_field` | `path` + `eq` / `ne` / `contains` / `gt` / `lt` / `exists` | 响应 JSON 某字段 |
| `response_text` | `contains` | 非 JSON / 文本响应 |

`emit` 字段的 `evidence` / `next_args` / `context` 都可用 Jinja2，可引用：
- `{{ response.X }}` — 完整响应 JSON
- `{{ params.X }}` — 调用参数
- `{{ status }}` — HTTP 状态码

---

## 5. HTTP API Connection 配置

admin 后台 → 接入管理 → 新增 → 选 **HTTP API**：

| 字段 | 说明 |
|---|---|
| `base_url` | 必填。Skill 的 path 会拼到这后面 |
| `auth_kind` | `none` / `bearer` / `basic` / `api_key_header` / `oauth2_client_credentials` |
| `bearer_token` | bearer 用；落库 Fernet 加密 |
| `basic_user` / `basic_pass` | basic 用 |
| `api_key` + `api_key_header_name` | 自定义 header 名 + 值（如 `X-Api-Key: ...`）|
| `oauth2_*` | Client Credentials Flow；token 自动缓存 + 401 时刷新 |
| `custom_headers` | 多行 `Key: Value`；和 skill 自身 headers 合并 |
| `verify_ssl` | 默认 true |
| `timeout_seconds` | 默认 30 |
| `proxy` | 可选；企业内网常用 |

所有凭证字段（`bearer_token` / `basic_pass` / `api_key` / `oauth2_client_secret`）
落到 `platform_connection.config_json` 时被 [`crypto.py`](../ops_platform/crypto.py) 透明加密。

---

## 6. 真实示例

### 6.1 Jira 搜 issue（POST + body_template + extract + signals）

```yaml
code: jira_search_issues
name: Jira 按 JQL 搜 issue
description: |
  按 JQL 搜 Jira issue。适用：用户提到 ticket id 想看上下文 / 查 X 项目最近 bug /
  看 X 人未完成 issue 等场景。返回 issue 关键字段（key / summary / status / assignee）。
  401 → auth_failure 信号。结果超过 100 条 → too_many_results 信号建议收窄 jql。
category: ticketing
connection_id: jira-prod
read_only: true
visibility: all

method: POST
path: /rest/api/3/search

body_template: |
  {
    "jql": "{{ jql }}",
    "maxResults": {{ limit | default(20) }},
    "fields": ["summary", "status", "assignee", "updated"]
  }

params_schema:
  type: object
  required: [jql]
  properties:
    jql: { type: string, description: "JQL 表达式" }
    limit: { type: integer, default: 20, maximum: 100 }

response_extract:
  total: '$.total'
  issue_keys: '$.issues[*].key'
  issues: '$.issues[*].fields'

signal_rules:
  - when: { type: http_status, eq: 401 }
    emit: { type: auth_failure, severity: critical, evidence: "Jira 401，凭证失效" }
  - when: { type: response_field, path: $.total, gt: 100 }
    emit:
      type: too_many_results
      severity: warning
      evidence: "结果 {{ response.total }} 条 > 100，建议收窄 jql"
```

### 6.2 GitLab 触发 pipeline（POST 写操作 + 二次确认）

```yaml
code: gitlab_trigger_pipeline
name: 触发 GitLab Pipeline
description: |
  对指定 project + ref 触发一次 CI Pipeline。**写操作**，需用户确认。
category: cicd
connection_id: gitlab-prod
read_only: false
requires_admin_approval: true        # 高危：可能触发部署
visibility: admin

method: POST
path: /api/v4/projects/{{ project_id | urlencode }}/pipeline

body_template: |
  { "ref": "{{ ref }}" }

params_schema:
  type: object
  required: [project_id, ref]
  properties:
    project_id: { type: string, description: "GitLab 项目 ID 或 group/path" }
    ref:        { type: string, description: "分支/Tag 名" }

response_extract:
  pipeline_id: '$.id'
  status: '$.status'
  web_url: '$.web_url'
```

### 6.3 内部 CMDB 查主机（GET + path 模板）

```yaml
code: cmdb_get_host_info
name: 从 CMDB 查主机详细信息
description: |
  按 hostname 查 CMDB 中的资产信息：业务负责人、机房、配置规格、维保状态。
  当 zabbix_get_host_overview 拿不到主机时，可以用本 skill 查 CMDB 看是不是已下线。
category: cmdb
connection_id: cmdb-internal
read_only: true

method: GET
path: /api/v1/hosts/{{ hostname | urlencode }}

params_schema:
  type: object
  required: [hostname]
  properties:
    hostname: { type: string }

response_extract:
  hostname: '$.hostname'
  owner: '$.business.owner'
  idc: '$.location.idc'
  status: '$.lifecycle.status'

signal_rules:
  - when: { type: http_status, eq: 404 }
    emit:
      type: host_not_in_cmdb
      severity: warning
      evidence: "CMDB 查不到 {{ params.hostname }}，可能已下线或拼写错"
  - when: { type: response_field, path: $.lifecycle.status, eq: "decommissioned" }
    emit:
      type: host_decommissioned
      severity: critical
      evidence: "{{ params.hostname }} 在 CMDB 标记为已下线"
```

---

## 7. 跟其它平台能力的协同

### 7.1 信号驱动跨域 pivot

HTTP skill 发出的信号和 Python skill 完全一致。例：CMDB 查询返回 `host_decommissioned`
→ runbook 引擎可以自动跳转到 `swarm_remove_service`（如果剧本里这么定义了）。

### 7.2 Runbook 编排

任何 HTTP skill 都可以被 [runbook 引擎](runbook.md) 引用：

```yaml
# 一个跨多个外部系统的诊断+处置剧本
nodes:
  fetch_cmdb:
    skill: cmdb_get_host_info
    args: { hostname: $user.hostname }

  search_jira:
    skill: jira_search_issues
    args:
      jql: "labels = 'host-{{ $nodes.fetch_cmdb.hostname }}' AND status != 'Done'"
    edges:
      - target: notify_oncall
        when: { type: response_field, path: $.total, gt: 0 }

  notify_oncall:
    skill: wechat_notify     # 又是一个 HTTP skill
    args:
      to_user: "{{ $nodes.fetch_cmdb.owner }}"
      message: "..."
```

### 7.3 写操作二次确认链

YAML 设 `read_only: false` → invoker 自动走 needs_confirmation 流程。
chainlit 弹卡片让用户确认；admin UI / MCP 同样支持。**和 Python 写 skill 路径完全一致**。

### 7.4 MCP 暴露

HTTP skill 自动出现在 MCP server 的 tool list。Claude Code / Cursor 通过 MCP 直接调，
看不到这是 YAML 还是 Python。

### 7.5 审计

每次调用都进 `platform_skill_call` 表，含 args / result / signals / latency / error。
和原生 skill 同表。

---

## 8. 调试建议

| 现象 | 排查 |
|---|---|
| 模型不调你的 HTTP skill | description 写得不够具体；改成"何时用 + 何时不用 + 信号→下一步" |
| 保存时返回 `connection 不存在` | 先在接入管理建一个 type=http_api 的 connection |
| 模板渲染失败 | "仅校验" 按钮会提前抓出 Jinja2 语法错；body_template 渲染后必须是合法 JSON |
| response_extract 拿不到 | 用 admin UI 调用一次（或在 chat 里问），看 audit 里完整的 raw response，再调整 path |
| 信号没触发 | 检查 `when.type` 拼写；模板里引用 `{{ response.X }}` 而不是 `{{ X }}` |

---

## 9. 路线图（Tier 2 & Tier 3）

当前的 Tier 1（YAML 声明）已经能解决 80% 的对接需求。剩下两层规划：

### Tier 2 · OpenAPI 批量导入（计划中）

很多现代系统都发 OpenAPI/Swagger 规范。让 admin 输入 spec URL，平台：

1. fetch + parse OpenAPI spec
2. 为每个 operation 自动生成 Tier 1 YAML 草稿
3. 草稿默认 `enabled: false`
4. admin 逐条 review/编辑 description（**不能省**——OpenAPI 的 summary 多半是机器风格，
   对模型 tool 选择不友好；description 要写"何时使用 + 信号→下一步"）
5. 启用想要的，删掉不要的

实现量预估 ~250 行 + 一个 admin UI 页面。等 Tier 1 实际接 3 个以上 OpenAPI 系统再做，
避免过度设计。

### 案例：把 Zabbix 接入也"统一"成 HTTP YAML（架构迁移路径）

Zabbix 本质就是 JSON-RPC HTTP API，理论上完全可以走 `http_api` driver + YAML skill。
平台已经 seed 了一条 `zabbix_jsonrpc` 通用 skill 作为示例（默认 disabled）。

**为什么不一刀切替换 Python ZabbixClient？**——分情况看：

| Skill 类型 | 走 YAML 行不行 | 推荐 |
|---|---|---|
| 单步 RPC（host.get / item.get / history.get / problem.get …）| ✅ 完美适合 | **`zabbix_jsonrpc` YAML 一个搞定**——admin 启用即可。 |
| 复合查询（host_overview = 找 host → 取 item → 拉 history → 算 avg/max）| ⚠️ 部分 | YAML 不能算 mean()。**Python skill 留着**做聚合，但每一步底层 RPC 可以改写成调 `zabbix_jsonrpc`，再用 runbook 编排。 |
| 自定义业务查询（"查 X 业务线最近一周告警次数"）| ✅ | admin 在后台直接写一份 YAML，复用 `zabbix_jsonrpc` 思路，零代码上线。 |

**启用 zabbix_jsonrpc 的步骤**：

1. 在 Zabbix 6.0+ 控制台：Administration → User → API tokens → 生成一个 token
2. 平台后台 → 接入管理 → 新增接入 → 选 **HTTP API**：
   - `base_url` = `http://your-zabbix/zabbix/api_jsonrpc.php`
   - `auth_kind` = `bearer`
   - `bearer_token` = 上一步生成的 token
   - 验证连通
3. 平台后台 → HTTP Skill → 编辑 `zabbix_jsonrpc`：
   - 把 `connection_id` 设为上一步建的 connection
   - `enabled: true`
   - 保存
4. 立即在 Chainlit 里能调：`zabbix_jsonrpc(method="host.get", params={...})`

注意：Zabbix 5.x 及以下不支持 API token + Bearer，必须先调 `user.login` 拿 auth token 再放到 body 里。这种情况要等 `http_api` driver 加 `zabbix_login` 这种自定义 auth flow 支持，或者继续用 Python `ZabbixClient`。

### Tier 3 · Remote MCP Connection（计划中）

未来如果对接的系统已经发了 MCP server（Atlassian、GitHub、Notion 等都在做），可以走：

1. admin 注册一个 type=`mcp_remote` 的 connection（URL + 鉴权）
2. 平台启动时通过 MCP client 协议调对方 `tools/list`
3. 把每个远端 tool 自动包装成本平台 skill（前缀 `mcp_<system>_<tool>`）
4. 调用时透明 forward 到远端 MCP server

**完全不写 YAML，零代码**。但生态目前还在早期，等成熟再做。
留 driver 接口位置即可。

---

## 10. 一句话

> 把"对接外部系统"从"工程师写代码"变成"admin 写 YAML"。
> 配置即能力，admin 是平台的扩展者，不是工程师的依赖方。
