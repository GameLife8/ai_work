# 告警智能研判系统设计文档（Flask 版 - 第一阶段）

> 历史文档：本文记录的是告警研判系统第一阶段的 Flask 设计稿。当前仓库已经演进为统一 AI 运维平台，
> 运行期架构、skill、runbook、确认续跑、Chainlit/Admin/MCP 三入口等请以
> [`README.md`](README.md) 和 [`docs/platform.md`](docs/platform.md) 为准。

## 1. 项目概述

本项目目标是构建一个基于 Zabbix 的告警智能研判系统，实现：

- 自动接收告警
- 自动补全上下文（本地执行）
- 调用远程 AI 进行决策
- 输出最终处理动作：
  - 推送
  - 延迟观察
  - 合并
  - 忽略

当前阶段只开发 **Python Flask 部分**。  
图数据库暂不实现，仅预留接口。

---

## 2. 系统约束

### 2.1 网络约束

- 内网可以访问公网
- 公网不能访问内网

结论：

- AI **不能主动调用本地接口**
- 必须由本地 Flask 服务**主动请求**远程 AI

---

### 2.2 数据访问约束

远程 AI **不能直接访问**：

- Zabbix
- TiDB
- 本地图关系服务
- 任意内网 API

因此必须采用：

- 本地代码查询数据
- 本地代码组装上下文
- 远程 AI 只返回：
  - 需要什么上下文
  - 最终判断结果

---

## 3. 基础环境

### 3.1 Zabbix

```text
http://169.24.2.90:80
```

### 3.2 TiDB

```text
host: 169.24.1.87
port: 4000
database: ai_alert
username: ai_alert
password: ai_alert
```

### 3.3 Python

```text
Python 3.13.12
```

---

## 4. 第一阶段范围

### 4.1 本阶段实现

- 接收 Zabbix 告警
- 解析告警基础信息
- 调用远程 AI Planner
- 本地查询上下文
- 调用远程 AI Judge
- 输出决策
- 保存原始告警
- 保存最终决策
- 保存/更新 incident
- 预留图关系查询接口

### 4.2 本阶段不实现

- 图数据库真实实现
- 多跳拓扑推理
- OTEL / SkyWalking
- 自学习
- AI 自由调用工具
- 复杂任务编排

---

## 5. 系统总体架构

```text
Zabbix
  ↓
Flask Alert Service（本地）
  ↓
调用远程 AI Planner
  ↓
本地查询上下文
  ├─ Zabbix API
  ├─ Incident 查询
  └─ Graph 接口（预留）
  ↓
调用远程 AI Judge
  ↓
本地执行动作
  ├─ 推送
  ├─ 延迟观察
  ├─ 合并
  └─ 忽略
  ↓
TiDB 存储
```

---

## 6. 核心流程

### 6.1 主流程

1. 接收一条 Zabbix 告警
2. 解析基础字段
3. 保存原始告警
4. 调用远程 AI Planner
5. 获取需要的上下文类型
6. 本地执行上下文查询
7. 组装上下文摘要
8. 调用远程 AI Judge
9. 获取最终决策
10. 保存决策结果
11. 更新或创建 incident
12. 执行动作（推送/延迟观察/合并/忽略）

---

## 7. 数据流设计

### 7.1 第一轮：Planner

本地 Flask 服务向远程 AI 发送基础告警：

```json
{
  "alert": {
    "alert_name": "CPU usage > 90%",
    "host_name": "app-prod-01",
    "host_ip": "10.0.0.12",
    "severity": "high",
    "tags": {
      "env": "prod",
      "service": "order-api"
    }
  }
}
```

远程 AI 返回需要的上下文：

```json
{
  "needs": [
    "metric_summary",
    "topology",
    "related_incidents"
  ]
}
```

### 7.2 第二轮：Judge

本地查询上下文后，再发给远程 AI：

```json
{
  "alert": {
    "alert_name": "CPU usage > 90%",
    "host_name": "app-prod-01",
    "host_ip": "10.0.0.12",
    "severity": "high"
  },
  "context": {
    "metric_summary": {
      "cpu_avg": 94.1,
      "cpu_max": 97.6,
      "load_avg": 18.2
    },
    "topology": {
      "services": ["order-api"],
      "cluster": "swarm-prod"
    },
    "related_incidents": []
  }
}
```

远程 AI 返回最终决策：

```json
{
  "decision": "notify",
  "priority": "P1",
  "reason": "CPU持续高且为生产环境"
}
```

---

## 8. 模块设计

## 8.1 Flask Alert Service

职责：

- 提供 Web API 接收 Zabbix 告警
- 串联整个处理流程
- 调用 AI
- 调用本地查询模块
- 落库
- 执行动作

---

## 8.2 Alert Parser

职责：

从 Zabbix 告警中提取基础字段：

- event_id
- trigger_id
- host_name
- host_ip
- severity
- alert_name
- tags
- event_time

输出标准化对象：

```python
{
    "source_event_id": "123456",
    "trigger_id": "98765",
    "host_name": "app-prod-01",
    "host_ip": "10.0.0.12",
    "severity": "high",
    "alert_name": "CPU usage > 90%",
    "tags": {"env": "prod"},
    "event_time": "2026-04-01T10:02:00"
}
```

---

## 8.3 AI Client

职责：

封装两个远程 AI 接口调用：

- `plan_context`
- `judge_alert`

说明：

- 只通过 HTTP 请求远程 AI
- AI 只返回 JSON
- AI 不接触内网资源

---

## 8.4 Context Fetcher

职责：

根据 Planner 返回的 `needs` 列表，本地执行上下文查询。

支持三类上下文：

- `metric_summary`
- `topology`
- `related_incidents`

---

## 8.5 Zabbix Client

职责：

封装对 Zabbix API 的访问。

第一阶段主要做：

- 获取时间窗口指标摘要
- 获取 trigger 相关信息（可选）
- 获取 host 相关信息（可选）

注意：

- 当前不要求存储完整时间序列
- 只返回摘要结果

---

## 8.6 Incident Service

职责：

- 查询同节点已有 incident
- 查询可合并 incident
- 创建 incident
- 更新 incident
- 建立 incident 与 alert 的关联

---

## 8.7 Graph Client（预留）

职责：

- 查询节点关系
- 查询主机/服务/集群关系
- 若将来接入 Go 图服务，可以直接替换此模块

当前阶段先返回默认结构：

```python
{
    "services": [],
    "cluster": None
}
```

---

## 8.8 Decision Engine

职责：

根据 AI Judge 结果执行：

- notify
- observe
- merge
- ignore

---

## 9. Flask 项目结构建议

```text
ai-alert-flask/
├── app.py
├── config.py
├── requirements.txt
├── routers/
│   └── alert_router.py
├── services/
│   ├── alert_parser.py
│   ├── ai_client.py
│   ├── context_fetcher.py
│   ├── zabbix_client.py
│   ├── incident_service.py
│   ├── decision_engine.py
│   └── graph_client.py
├── models/
│   ├── db.py
│   ├── alert_event.py
│   ├── alert_decision.py
│   ├── incident.py
│   └── incident_alert_rel.py
└── utils/
    └── logger.py
```

---

## 10. 接口设计

## 10.1 接收 Zabbix 告警

### URL

```text
POST /api/v1/alerts/zabbix
```

### 请求体示例

```json
{
  "event_id": "123456",
  "problem_id": "654321",
  "trigger_id": "98765",
  "host_id": "10001",
  "host_name": "app-prod-01",
  "host_ip": "10.0.0.12",
  "severity": "high",
  "status": "problem",
  "alert_name": "CPU usage > 90%",
  "message": "CPU usage high",
  "tags": {
    "env": "prod",
    "service": "order-api"
  },
  "event_time": "2026-04-01T10:02:00"
}
```

### 返回体示例

```json
{
  "code": 0,
  "message": "accepted",
  "alert_id": 1000001
}
```

---

## 10.2 远程 AI Planner 接口

### URL

```text
POST {AI_BASE_URL}/plan_context
```

### 请求体

```json
{
  "alert": {
    "alert_name": "CPU usage > 90%",
    "host_name": "app-prod-01",
    "host_ip": "10.0.0.12",
    "severity": "high",
    "tags": {
      "env": "prod"
    }
  }
}
```

### 返回体

```json
{
  "needs": [
    "metric_summary",
    "topology",
    "related_incidents"
  ]
}
```

---

## 10.3 远程 AI Judge 接口

### URL

```text
POST {AI_BASE_URL}/judge_alert
```

### 请求体

```json
{
  "alert": {
    "alert_name": "CPU usage > 90%",
    "host_name": "app-prod-01",
    "host_ip": "10.0.0.12",
    "severity": "high"
  },
  "context": {
    "metric_summary": {
      "cpu_avg": 94.1,
      "cpu_max": 97.6,
      "load_avg": 18.2
    },
    "topology": {
      "services": ["order-api"],
      "cluster": "swarm-prod"
    },
    "related_incidents": []
  }
}
```

### 返回体

```json
{
  "decision": "notify",
  "priority": "P1",
  "reason": "CPU持续高且为生产环境"
}
```

---

## 11. 本地内部函数设计

## 11.1 `fetch_context(needs, alert)`

职责：

- 根据 `needs` 依次查询本地上下文
- 返回统一 context 结构

示例：

```python
def fetch_context(needs, alert):
    context = {}

    if "metric_summary" in needs:
        context["metric_summary"] = get_metric_summary(alert)

    if "topology" in needs:
        context["topology"] = resolve_topology(alert)

    if "related_incidents" in needs:
        context["related_incidents"] = get_related_incidents(alert)

    return context
```

---

## 11.2 `get_metric_summary(alert)`

职责：

- 调 Zabbix API
- 获取过去一段时间的指标摘要
- 返回均值、最大值等

返回示例：

```json
{
  "cpu_avg": 94.1,
  "cpu_max": 97.6,
  "load_avg": 18.2
}
```

---

## 11.3 `resolve_topology(alert)`

职责：

- 预留图关系查询接口
- 未来接 Go 图服务
- 当前返回默认结构

返回示例：

```json
{
  "services": [],
  "cluster": null
}
```

---

## 11.4 `get_related_incidents(alert)`

职责：

- 查询 TiDB 中同 host / 同 IP / 同 service 的 open incident
- 返回可合并 incident 列表

返回示例：

```json
[
  {
    "incident_no": "INC202604010021",
    "status": "open",
    "priority": "P2"
  }
]
```

---

## 12. TiDB 数据存储设计

第一阶段只保留以下表。

---

## 12.1 `alert_event`

保存原始告警。

```sql
CREATE TABLE alert_event (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    source VARCHAR(32) NOT NULL DEFAULT 'zabbix',
    source_event_id VARCHAR(128),
    source_problem_id VARCHAR(128),
    trigger_id VARCHAR(128),
    host_id VARCHAR(128),
    host_name VARCHAR(255),
    host_ip VARCHAR(64),
    severity VARCHAR(32),
    status VARCHAR(32),
    alert_name VARCHAR(512),
    alert_message TEXT,
    tags_json JSON,
    event_time DATETIME,
    ingest_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    raw_payload_json JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 12.2 `alert_decision`

保存最终研判结果。

```sql
CREATE TABLE alert_decision (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    alert_event_id BIGINT NOT NULL,
    incident_no VARCHAR(128),
    decision_type VARCHAR(32) NOT NULL,
    priority VARCHAR(32),
    need_push TINYINT NOT NULL DEFAULT 0,
    reason_summary TEXT,
    merge_target_incident_no VARCHAR(128),
    observe_until DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 12.3 `incident`

保存收敛后的问题对象。

```sql
CREATE TABLE incident (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    incident_no VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    priority VARCHAR(32),
    root_alert_event_id BIGINT,
    root_node_key VARCHAR(255),
    summary TEXT,
    first_seen_at DATETIME,
    last_seen_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_incident_no (incident_no)
);
```

---

## 12.4 `incident_alert_rel`

incident 与 alert 的关联表。

```sql
CREATE TABLE incident_alert_rel (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    incident_no VARCHAR(128) NOT NULL,
    alert_event_id BIGINT NOT NULL,
    rel_type VARCHAR(32) NOT NULL DEFAULT 'member',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 13. 配置设计

## 13.1 `config.py`

建议配置项：

```python
ZABBIX_BASE_URL = "http://169.24.2.90:80/api_jsonrpc.php"

TIDB_HOST = "169.24.1.87"
TIDB_PORT = 4000
TIDB_DATABASE = "ai_alert"
TIDB_USERNAME = "ai_alert"
TIDB_PASSWORD = "ai_alert"

AI_BASE_URL = "https://your-ai-api"
AI_TIMEOUT_SECONDS = 30
```

---

## 14. 依赖建议

`requirements.txt` 建议内容：

```text
Flask==3.0.3
SQLAlchemy==2.0.36
PyMySQL==1.1.1
requests==2.32.3
python-dotenv==1.0.1
gunicorn==23.0.0
```

如果开发调试需要：

```text
pytest==8.3.3
```

---

## 15. 主流程伪代码

```python
def handle_alert(raw_payload):
    # 1. 解析
    alert = parse_alert(raw_payload)

    # 2. 保存原始告警
    alert_event_id = save_alert_event(alert, raw_payload)

    # 3. 调 AI Planner
    plan = plan_context(alert)
    needs = plan.get("needs", [])

    # 4. 本地查询上下文
    context = fetch_context(needs, alert)

    # 5. 调 AI Judge
    decision = judge_alert(alert, context)

    # 6. 保存决策
    save_alert_decision(alert_event_id, decision)

    # 7. 更新 incident
    update_incident(alert_event_id, alert, decision)

    # 8. 执行动作
    execute_action(alert, decision)

    return {
        "alert_event_id": alert_event_id,
        "decision": decision
    }
```

---

## 16. 动作定义

## 16.1 `notify`

条件：
- AI 判断为 `notify`

动作：
- 推送通知
- 创建或更新 incident

---

## 16.2 `observe`

条件：
- AI 判断为 `observe`

动作：
- 保存观察结果
- 设置 `observe_until`
- 暂不推送

---

## 16.3 `merge`

条件：
- AI 判断为 `merge`

动作：
- 保存合并目标
- 将 alert 关联到已有 incident
- 不新建 incident

---

## 16.4 `ignore`

条件：
- AI 判断为 `ignore`

动作：
- 记录原因
- 不推送
- 不进入新 incident

---

## 17. 异常与降级处理

## 17.1 AI Planner 失败

降级策略：

- 使用默认 needs：

```python
["metric_summary", "topology", "related_incidents"]
```

---

## 17.2 上下文查询失败

降级策略：

- 某项失败时用空结构代替
- 继续调用 AI Judge
- 或回退到本地兜底规则

---

## 17.3 AI Judge 失败

降级策略：

- 使用本地默认规则：
  - high + prod → notify
  - 其他 → observe

---

## 18. Codex 自动生成代码建议

可以直接给 Codex 以下提示：

```text
请帮我生成一个 Flask 项目，要求：

1. Python 3.13
2. 接收 Zabbix 告警：POST /api/v1/alerts/zabbix
3. 解析告警字段
4. 保存 alert_event 到 TiDB
5. 调用远程 AI Planner（plan_context）
6. 根据返回 needs 执行本地上下文查询
7. 调用远程 AI Judge（judge_alert）
8. 保存 alert_decision
9. 创建或更新 incident
10. 图数据库部分先不实现，只保留 graph_client.py 预留接口

数据库：
host=169.24.1.87
port=4000
database=ai_alert
username=ai_alert
password=ai_alert

Zabbix:
http://169.24.2.90:80

请按 Flask 项目结构生成：
- app.py
- config.py
- routers
- services
- models
- requirements.txt
```

---

## 19. 第一阶段交付物

第一阶段建议交付：

- Flask API 服务
- TiDB 建表 SQL
- Zabbix Client 基础封装
- AI Client 封装
- 告警主处理流程
- incident 管理逻辑
- Graph 接口预留

---

## 20. 总结

当前系统采用最简、最稳的方式：

- 本地 Flask 服务接收告警
- 本地主动调用远程 AI
- AI 先规划需要什么上下文
- 本地去查数据
- AI 再做判断
- 本地执行动作并存储结果

这套方式适合当前内网单向访问公网的网络条件，也方便后续扩展图关系服务和更多上下文来源。
