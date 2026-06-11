# 诊断剧本（Runbook）· 图执行引擎

> 让"运维老司机的查问题套路"代码化成可被平台自动执行的有向图。
> 模型只负责语言层（写中文报告），不再做路径决策。

代码：[`ops_platform/runbook_engine.py`](../ops_platform/runbook_engine.py) · [`ops_platform/runbook_seeds.py`](../ops_platform/runbook_seeds.py) · [`skills/platform_run_runbook`](../skills/platform_run_runbook/__init__.py)

---

## 1. 为什么要图执行

模型让自己挑 skill 的诊断路径**每次都不太一样**——同一个问题：
- 可能漏查跨域（容器问题没追到主机层）
- 可能反复试关键词
- 可能在 token 用尽前没收完证据

把诊断套路**代码化**成可执行图，平台来保证：

1. **跨域 pivot 一定发生**（OOM 信号 → 必查 zabbix host）
2. **同一个 skill 不会被重复调**
3. **失败节点按 on_error 策略优雅降级**
4. **每一步都落审计**，事后可逐节点回放

模型在末端只做**语言层**：把所有证据转成中文五段式报告。

---

## 2. 整体架构

```
                          Runbook（DB 中的图定义）
                                  │
                                  ▼ admin 改完热加载
                       ┌────RunbookRegistry────┐
                       │（内存缓存所有 runbook）│
                       └──────────┬─────────────┘
                                  │
   user 提问 ───agent───▶ platform_run_runbook(user_query, inputs)
                                  │
                                  ▼
                          RunbookExecutor.execute()
                                  │
            ┌─── BFS 调度 + visited 去重 + deadline ──┐
            │                                          │
            ▼                                          ▼
      Reference 解析                           Condition 评估
   $user / $nodes / $signals          has_signal / status_ok / 比较
            │                                          │
            └──── Skill 调用（带 timeout/retry）────────┘
                                  │
                                  ▼
                       收集 signals → 驱动下一跳
                                  │
                                  ▼
                          ExecutionResult
                                  │
                                  ▼
                     模型基于此写五段式中文报告
                                  │
                                  ▼
                  落库 platform_runbook_execution（可回放）
```

---

## 3. Runbook 定义

每个剧本是一份 YAML/JSON 定义；admin 后台可视化编辑。完整字段：

```yaml
key: swarm_service_not_starting          # 全局唯一 ID
title: Swarm 服务起不来 / 持续重启
description: ...
triggers:                                 # 用户问题命中任意一个 → 自动选中本剧本
  - 起不来
  - 启动失败
  - 不停重启
inputs:                                   # 必填用户输入
  - service_name
optional_inputs:                          # 选填
  - namespace
start_node: health                        # 起始节点
max_total_seconds: 240                    # 全局执行 deadline

# 自定义最终报告 prompt（不填用平台默认五段式）
final_report_prompt: |
  你是运维助手，请用五段式中文报告...

nodes:
  health:                                 # 节点 ID
    skill: swarm_check_service_health     # 必须是已注册的只读 skill
    description: 总览：副本/更新状态/失败任务
    args:                                 # 参数支持引用语法
      service_name: $user.service_name
    timeout_seconds: 60                   # 节点级超时
    retry: 0                              # 失败重试次数
    on_error: fail                        # fail | skip | continue
    if_when:                              # 节点级守卫；不满足跳过
      type: ...
    edges:                                # 出边列表
      - target: tasks                     # 下一节点 id
        when:                             # 边条件；不满足这条边不走
          type: has_signal
          signal_type: oom_kill
        label: "if oom_kill"              # 仅给 admin 后台看
```

### 3.1 引用语法（args + condition.path 都能用）

| 形式 | 含义 | 例子 |
|---|---|---|
| `$user.<field>` | 用户传入的 inputs | `$user.service_name` |
| `$nodes.<id>.<jsonpath>` | 上游节点的 result | `$nodes.tasks.failed_tasks[0].Node` |
| `$signals.<type>.<path>` | 全局信号集中第一条匹配 type 的字段 | `$signals.oom_kill.context.node` |
| 字面值 | 不以 `$` 开头的字符串/数字/布尔 | `"error"` / `42` / `true` |

`None` 值会被 args 解析自动剔除，让 skill 走平台默认（比如 `connection_id` 自动注入）。

### 3.2 条件类型

| type | 含义 | 字段 |
|---|---|---|
| `has_signal` | 全局信号集中是否含某 type | `signal_type` |
| `signal_severity_at_least` | 信号严重度门槛 | `signal_type`, `severity` (info/warning/critical) |
| `status_ok` / `status_failed` | 上游节点状态 | `node` |
| `field_eq` / `field_ne` / `field_contains` | 字段精确比较 | `path` (一个 ref), `value` |
| `field_gt` / `field_lt` | 数值比较 | `path`, `value` |
| `any_of` / `all_of` | 复合 OR / AND | `conditions: [...]` |
| `not` | 取反 | `conditions: [<single>]` |

---

## 4. 执行语义（按场景）

### 4.1 顺序执行
```yaml
nodes:
  a:
    edges: [{target: b}]
  b:
    edges: [{target: c}]
  c: {}
```
A → B → C 严格按序。

### 4.2 条件分支
```yaml
nodes:
  tasks:
    skill: swarm_get_failed_tasks
    edges:
      - target: host_overview        # 只在 OOM 信号出现时走
        when: {type: has_signal, signal_type: oom_kill}
      - target: host_storage         # 只在磁盘满信号出现时走
        when: {type: has_signal, signal_type: no_space_left}
      - target: logs_error           # 兜底（无 when → 永远走）
```
信号驱动跨域 pivot 的标准写法。

### 4.3 节点级守卫（if_when）
```yaml
nodes:
  describe:
    skill: k8s_describe_pod
    args: {name: $user.pod_name}
    if_when:                         # pod_name 没传就跳过此节点
      type: field_ne
      path: $user.pod_name
      value: null
```
状态会标 `skipped` 而不是 `pending`。

### 4.4 失败处理
| `on_error` | 行为 |
|---|---|
| `fail`（默认）| 节点失败 → 整个 runbook 终止，全局 status=failed |
| `skip` | 节点失败 → 继续走它的边到下游 |
| `continue` | 同 skip（语义保留） |

配 `retry: 2` 可在失败时自动重试 2 次。

### 4.5 超时
- `timeout_seconds`（每节点）—— 用 `ThreadPoolExecutor.future.result(timeout=)` 实现；超时节点状态 `timeout`
- `max_total_seconds`（全局，剧本根级）—— 每轮 BFS 检查 deadline；超过则全局 `timeout` 中止

### 4.6 写 skill 禁止入图
**任何 `read_only=False` 的 skill 在加载校验时直接拒绝**。写操作必须经过 `needs_confirmation` 链路，不能在自动执行中悄悄发生。

### 4.7 环检测
DAG 加载时跑 DFS 染色法找环，发现立即拒绝并报错路径 `a → b → a`。

---

## 5. 信号驱动跨域

每个 skill 在返回时可附带 `_signals` 数组（见 [`ops_platform/signals.py`](../ops_platform/signals.py)）：

```python
{
  "type": "oom_kill",                    # 19 种内置类型
  "severity": "critical",                # info/warning/critical
  "evidence": "任务 X 在节点 W1 退出码 137",
  "next_skill": "zabbix_get_host_overview",
  "next_args": {"host_query": "W1"},
  "context": {"node": "W1"}              # 给后续 skill 用的元数据
}
```

执行器自动：
1. 把每节点的 signals 收集进 `ExecutionContext.collected_signals`（去重）
2. 让 `$signals.<type>.<path>` 引用解析能拿到
3. 让 `has_signal` / `signal_severity_at_least` 条件能评估

**这就是跨域 pivot 从软约束变硬约束的机制**。

---

## 6. 自动匹配（trigger）

`platform_run_runbook(user_query="...")` **不指定 name** 时：

1. 平台遍历所有启用的 runbook
2. 按 `triggers` 关键词在 `user_query` 中的命中数排序
3. 命中数最多的胜出
4. 全部 0 命中 → 返回 `error=no_matching_runbook`，模型回退到自己挑 skill

例：
- "iiot-haitu_seatable 起不来" → `swarm_service_not_starting`（命中"起不来"）
- "worker-3 上 80 端口连不上" → `network_troubleshooting`（命中"连不上"）
- "我想吃晚饭" → 无匹配

---

## 7. 默认出厂剧本

平台首启时往 `platform_runbook` 表 seed 5 条**可执行 DAG**（详见 [`runbook_seeds.py`](../ops_platform/runbook_seeds.py)）：

| key | 触发 | 节点 | 关键 pivot |
|---|---|---|---|
| `swarm_service_not_starting` | 起不来 / 不停重启 / CrashLoop | swarm_query | OOM/磁盘/镜像三向 pivot |
| `k8s_pod_crashloop` | CrashLoopBackOff / OOMKilled / Evicted | kube_query | 信号驱动 host_overview / host_storage |
| `host_resource_alert` | 主机CPU高 / 内存高 / 磁盘满 | zabbix | 概览 + 磁盘各挂载点 |
| `cluster_health_audit_swarm` | swarm 巡检 / 集群体检 | swarm_cluster_overview | 一把口聚合（节点+服务+监控） |
| `cluster_health_audit_k8s` | k8s 巡检 / 集群体检 | k8s_cluster_overview | 一把口聚合（节点+异常 pod+监控） |

**admin 改过的剧本不会被 seed 覆盖**——只在表为空时 seed。

> **可执行 DAG 只能编排只读 skill**——写 skill（`host_run_command` 等）一旦泄进自动执行，引擎会硬拦（见 [`runbook_engine.py`](../ops_platform/runbook_engine.py) `needs_confirmation` 检查）。所以「进容器 `nsenter` 做 DNS/连通性诊断」这类必须走 `host_run_command`(写) 的场景**不做成 DAG**，而是写成**文本剧本**（`container_netns_diag` / `network_troubleshooting` / `node_health_audit`，在 [`ops_platform/runbooks.py`](../ops_platform/runbooks.py)），由 `platform_get_runbooks` 返回，模型读着用 `host_run_command` 自己一步步跑。
>
> 为了防止模型读完文本剧本后仍反复 `swarm_query`，agent 对“服务/容器能否访问 IP:PORT”类问题加了保护：只要 trace 已通过 `swarm_query(category=service, verb=ps)` 找到运行中的任务和 Node，下一轮会临时只暴露 `host_run_command` 并强制 tool call，确保进入 `needs_confirmation` 确认流。

---

## 8. 模型集成

### 8.1 模型怎么用 runbook

`ops_platform/prompts.py` 的 workflow 段落明确告诉模型：

> 复合问题 / 不熟悉的场景：第一步直接调 `platform_run_runbook(user_query, inputs)`。
> 平台会自动按图取证；返回里 `final_report` 已经是中文五段式。
> **把 final_report 直接给用户即可，不要再追加任何 tool 调用**。
>
> 简单单点查询：直接挑对应 skill。

### 8.2 真实模型实测（历史样例，2026-05-02）

火山方舟 Code Plan + `ark-code-latest`（control 台后台决定底层模型，本次解析为 doubao-seed-1.8）。下面是图执行设计早期的真实输出样例，部分 skill 名称已在后续版本收口到 `swarm_query` / `kube_query` / `host_run_command`，但流程语义不变：

```
user: iiot-haitu_seatable 这个服务最近一直起不来，帮我看看根因

模型决策 → platform_run_runbook(user_query=..., inputs={service_name: ...})
平台执行：
  ✓ swarm_check_service_health  → 副本 0/3，emit replicas_insufficient + oom_kill
  ✓ swarm_get_failed_tasks       → 137 退出码，emit oom_kill
  ⚡ 跨域 pivot 命中
  ✓ zabbix_get_host_overview     → 节点 WYY-DOCK02 内存 96.4%
  ✓ swarm_get_service_logs_filter → java.lang.OutOfMemoryError

模型生成中文五段式报告（节选）：
### 当前状态
服务 0/3 副本运行；节点 WYY-DOCK02 内存使用率 96.4%。
### 关键证据
- Replicas: 0/3
- Error: task: non-zero exit (137)
- memory.used_pct: 96.4%, available_mb: 580
### 建议操作
1. 清理 WYY-DOCK02 上非必要容器
2. 调整调度策略迁到内存充足节点
3. 长期扩容物理内存
如需强制重启，可点击下方确认执行 swarm_force_update_service。

总耗时 84.8s，跨域 pivot 100% 命中，落库 1 条执行历史。
```

---

## 9. Admin UI

两个新页面：

- **诊断剧本**（`/runbooks`）—— admin 列表 + YAML 编辑器（CodeMirror）+ 校验按钮 + 删除/启停。**改完即时热加载**，无需重启。
- **剧本执行历史**（`/runbook-runs`）—— 每次 `platform_run_runbook` 调用的完整轨迹，按节点回放（args / result / signals / 耗时），最终报告全文展示。

---

## 10. 添加新剧本的步骤

1. **在 admin 后台 → 诊断剧本 → 新增剧本**
2. **写 key + title + triggers + inputs**
3. **在 YAML 编辑器里写 nodes**（参考默认剧本的写法）
4. **点"仅校验"**——后端跑 cycle detection / 写 skill 禁用 / skill 存在性检查；不通过会返回错误清单
5. **点"保存"**——通过校验后写库 + 热加载，下一次会话立即生效

---

## 11. 调试 / 排错

| 现象 | 排查 |
|---|---|
| 模型不调 platform_run_runbook | 检查 user_query 是否命中某剧本的 triggers；或在 prompts 里调 workflow 段强化引导 |
| Runbook 跑了但全局 status=failed | 看 abort_reason；多半是某节点 on_error=fail 触发 |
| 节点 status=skipped | if_when 守卫不满足；多半是 $user.X 没传 |
| 跨域 pivot 没触发 | 检查上游 skill 是否真的 emit 了对应 signal；signal 类型大小写要对 |
| 连通性问题只看到 swarm_query、不弹确认 | 确认用户问题里有服务/容器 + IP:PORT；trace 必须先查到 `service ps` 的 Running Node，之后 agent 才会强制进入 `host_run_command` 确认流 |
| YAML 保存失败 | 后端返回 `errors` 数组里有具体哪条 |

---

## 12. Roadmap

✅ 已完成：DAG 调度、refs/conditions DSL、timeout/retry/on_error、写 skill 禁用、环检测、信号自动驱动、热加载、admin UI、执行回放

🚧 计划：
- 节点并行执行（fan-out）
- 节点级 model_extract（让模型从前节点结果里抽某个值给下游用）
- 告警自动诊断（alert → 自动选 runbook → 推送给值班）
- runbook 版本回滚
