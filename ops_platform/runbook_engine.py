"""Runbook 图执行引擎。

把"诊断剧本"从给模型读的文字资料 → 升级成 **平台可自动执行的有向图**。

架构总览
========

::

    Runbook（DAG 定义）
        │
        ▼
    Validator（加载时校验：DAG 无环、skill 存在、写 skill 禁用、入参齐全）
        │
        ▼
    RunbookExecutor.execute(user_inputs, skill_ctx)
        │
        ├── 解析每个 node 的 args（$user.X / $nodes.id.path / $signals.type.field）
        ├── 评估 if_when 条件 → 跳过 / 执行
        ├── 调 invoker 跑 skill（带 timeout/retry）
        ├── 收集 signals 进 ExecutionContext
        ├── 评估 edge.when 条件 → 决定下一批 nodes
        └── BFS 走完整张图（visited 防重入，deadline 防失控）
              │
              ▼
          ExecutionResult（每节点状态 + 全局信号 + 报告候选数据）
              │
              ▼
          Model 写最终五段式中文报告

设计原则
========

1. **声明式 DAG，命令式执行**：admin 写 YAML 描述图，平台决定调用顺序与参数；
   模型只在最末端写报告（语言层），不再做路径决策（确定性层）。
2. **写 skill 禁止入图**：写操作必须经过 needs_confirmation 链路，不能在自动执行中悄悄发生。
3. **失败优雅**：节点级 on_error 控制（fail/skip/continue），全局有 deadline 兜底。
4. **可审计**：每次执行落 ``platform_runbook_execution`` 表，含每节点的 args/result/signals/latency。
5. **可演进**：runbook 是 DB 数据不是代码——admin 后台编辑、版本号自增、热加载。

关键数据流
==========

::

    user → "iiot-haitu_seatable 起不来"
        ↓
    agent 决定调 platform_run_runbook(name=auto, user_query=..., inputs={service_name: ...})
        ↓
    runbook_registry.match() → swarm_service_not_starting
        ↓
    RunbookExecutor.execute() ⤵
        node[health]   → swarm_check_service_health
        node[tasks]    → swarm_get_failed_tasks → 信号:oom_kill,node=W1
        edge[oom_kill] → 走向 host_overview
        node[host_overview] → zabbix_get_host_overview(host_query=W1)
        edge[default]  → 走向 logs
        node[logs]     → swarm_get_service_logs_filter(keyword=error)
        ↓
    ExecutionResult → model 写报告
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeoutError
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from ops_platform.signals import collect as collect_signals


logger = logging.getLogger(__name__)


# ============================================================================
# 1. 数据类（Runbook 定义结构）
# ============================================================================

@dataclass
class RunbookCondition:
    """节点 / 边的布尔条件。

    支持的 ``type``：
      - has_signal                 需要 signal_type
      - signal_severity_at_least   需要 signal_type + severity (info/warning/critical)
      - status_ok                  需要 node（指向上游节点 id）
      - status_failed              需要 node
      - field_eq / field_ne        需要 path（ref 字符串）+ value
      - field_contains             needs path + value
      - field_gt / field_lt        需要 path + value，按 float 比较
      - any_of / all_of            复合，需要 conditions 列表
      - not                        需要 conditions[0]

    None 等价于 always-true。
    """

    type: str
    signal_type: str | None = None
    severity: str | None = None
    node: str | None = None
    path: str | None = None
    value: Any = None
    conditions: list["RunbookCondition"] = field(default_factory=list)

    @classmethod
    def parse(cls, data: Any) -> "RunbookCondition | None":
        if data is None:
            return None
        if not isinstance(data, dict):
            raise ValueError(f"condition 必须是 dict 或 null，got {type(data).__name__}")
        kind = data.get("type")
        if not kind:
            raise ValueError("condition 缺少 type 字段")
        sub = [cls.parse(c) for c in (data.get("conditions") or [])]
        sub = [c for c in sub if c is not None]
        return cls(
            type=kind,
            signal_type=data.get("signal_type"),
            severity=data.get("severity"),
            node=data.get("node"),
            path=data.get("path"),
            value=data.get("value"),
            conditions=sub,
        )


@dataclass
class RunbookEdge:
    """从一个 node 指向下一个 node 的边。"""

    target: str
    when: RunbookCondition | None = None
    label: str = ""


@dataclass
class RunbookNode:
    """图中一个节点 = 一次 skill 调用。"""

    id: str
    skill: str
    description: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 60
    retry: int = 0
    on_error: str = "fail"     # fail | skip | continue
    if_when: RunbookCondition | None = None
    edges: list[RunbookEdge] = field(default_factory=list)


@dataclass
class Runbook:
    """完整剧本定义。"""

    key: str
    title: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)         # 必填的 user 输入字段名
    optional_inputs: list[str] = field(default_factory=list)
    start_node: str = ""
    nodes: dict[str, RunbookNode] = field(default_factory=dict)
    final_report_prompt: str = ""
    max_total_seconds: int = 300
    version: int = 1
    enabled: bool = True

    def to_admin_dict(self) -> dict[str, Any]:
        """admin 后台展示用（含全部字段）。"""
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "triggers": self.triggers,
            "inputs": self.inputs,
            "optional_inputs": self.optional_inputs,
            "start_node": self.start_node,
            "nodes": {
                nid: {
                    "id": n.id,
                    "skill": n.skill,
                    "description": n.description,
                    "args": n.args,
                    "timeout_seconds": n.timeout_seconds,
                    "retry": n.retry,
                    "on_error": n.on_error,
                    "if_when": _condition_to_dict(n.if_when),
                    "edges": [
                        {"target": e.target, "label": e.label,
                         "when": _condition_to_dict(e.when)}
                        for e in n.edges
                    ],
                } for nid, n in self.nodes.items()
            },
            "final_report_prompt": self.final_report_prompt,
            "max_total_seconds": self.max_total_seconds,
            "version": self.version,
            "enabled": self.enabled,
        }


def _condition_to_dict(c: RunbookCondition | None) -> dict | None:
    if c is None:
        return None
    out = {"type": c.type}
    for k in ("signal_type", "severity", "node", "path", "value"):
        v = getattr(c, k)
        if v is not None:
            out[k] = v
    if c.conditions:
        out["conditions"] = [_condition_to_dict(x) for x in c.conditions]
    return out


# ============================================================================
# 2. Loader / Validator
# ============================================================================

VALID_ON_ERROR = {"fail", "skip", "continue"}
VALID_CONDITION_TYPES = {
    "has_signal", "signal_severity_at_least",
    "status_ok", "status_failed",
    "field_eq", "field_ne", "field_contains", "field_gt", "field_lt",
    "any_of", "all_of", "not",
}


class RunbookLoadError(Exception):
    """Runbook 解析 / 校验失败。"""


def load_runbook_from_dict(definition: dict[str, Any]) -> Runbook:
    """从 dict（YAML 或 JSON 解析后）构造 Runbook 对象。"""
    if not isinstance(definition, dict):
        raise RunbookLoadError("定义必须是 dict")
    key = definition.get("key")
    title = definition.get("title")
    if not key:
        raise RunbookLoadError("缺少必填字段 key")
    if not title:
        raise RunbookLoadError(f"runbook {key} 缺少 title")

    nodes_raw = definition.get("nodes") or {}
    if not isinstance(nodes_raw, dict) or not nodes_raw:
        raise RunbookLoadError(f"runbook {key} 必须至少有一个 node")

    nodes: dict[str, RunbookNode] = {}
    for nid, ndata in nodes_raw.items():
        if not isinstance(ndata, dict):
            raise RunbookLoadError(f"node {nid} 必须是 dict")
        skill = ndata.get("skill")
        if not skill:
            raise RunbookLoadError(f"node {nid} 缺少 skill")
        on_error = ndata.get("on_error", "fail")
        if on_error not in VALID_ON_ERROR:
            raise RunbookLoadError(f"node {nid} on_error={on_error} 不合法（{VALID_ON_ERROR}）")
        try:
            if_when = RunbookCondition.parse(ndata.get("if_when") or ndata.get("if"))
        except ValueError as e:
            raise RunbookLoadError(f"node {nid} if_when 解析失败：{e}") from e

        edges: list[RunbookEdge] = []
        for e in ndata.get("edges") or []:
            if not isinstance(e, dict):
                raise RunbookLoadError(f"node {nid} edges 元素必须是 dict")
            tgt = e.get("target")
            if not tgt:
                raise RunbookLoadError(f"node {nid} 某条 edge 缺少 target")
            try:
                when = RunbookCondition.parse(e.get("when"))
            except ValueError as exc:
                raise RunbookLoadError(f"node {nid} → {tgt} when 解析失败：{exc}") from exc
            edges.append(RunbookEdge(target=tgt, when=when, label=e.get("label", "")))

        nodes[nid] = RunbookNode(
            id=nid,
            skill=skill,
            description=ndata.get("description", ""),
            args=dict(ndata.get("args") or {}),
            timeout_seconds=int(ndata.get("timeout_seconds") or 60),
            retry=int(ndata.get("retry") or 0),
            on_error=on_error,
            if_when=if_when,
            edges=edges,
        )

    start_node = definition.get("start_node") or next(iter(nodes.keys()))

    rb = Runbook(
        key=key,
        title=title,
        description=definition.get("description", ""),
        triggers=list(definition.get("triggers") or []),
        inputs=list(definition.get("inputs") or []),
        optional_inputs=list(definition.get("optional_inputs") or []),
        start_node=start_node,
        nodes=nodes,
        final_report_prompt=definition.get("final_report_prompt", ""),
        max_total_seconds=int(definition.get("max_total_seconds") or 300),
        version=int(definition.get("version") or 1),
        enabled=bool(definition.get("enabled", True)),
    )
    return rb


def validate_runbook(
    rb: Runbook,
    *,
    known_skills: set[str] | None = None,
    write_skills: set[str] | None = None,
) -> list[str]:
    """返回错误列表；空 → 校验通过。"""
    errors: list[str] = []

    if rb.start_node not in rb.nodes:
        errors.append(f"start_node '{rb.start_node}' 不在 nodes 列表里")

    # 边 target 必须存在
    for nid, n in rb.nodes.items():
        for e in n.edges:
            if e.target not in rb.nodes:
                errors.append(f"node {nid} → {e.target}：target 不存在")

    # 写 skill 禁止入图
    if write_skills:
        for nid, n in rb.nodes.items():
            if n.skill in write_skills:
                errors.append(
                    f"node {nid}：skill '{n.skill}' 是写操作，禁止在 runbook 中自动执行"
                )

    # skill 必须已注册
    if known_skills is not None:
        for nid, n in rb.nodes.items():
            if n.skill not in known_skills:
                errors.append(f"node {nid}：skill '{n.skill}' 未注册")

    # 环检测
    cycle = _detect_cycle(rb)
    if cycle:
        errors.append(f"图存在环：{' → '.join(cycle)}")

    # 引用语法的轻量校验（只看一层 $）
    # 注意：resolve_ref 支持 ``$a||$b||literal`` fallback 语法（见函数文档），
    # validator 必须跟上——把 ref 按 ``||`` 拆开，逐段校验 $-开头的 token，
    # 字面值 token 跳过。否则带 ``||`` 的 runbook 会被误判为非法引用。
    for nid, n in rb.nodes.items():
        for k, v in n.args.items():
            for ref in _iter_refs(v):
                tokens = [t.strip() for t in ref.split("||")] if "||" in ref else [ref]
                for tok in tokens:
                    if not tok or not tok.startswith("$"):
                        continue   # 空段或字面值兜底,不需要按引用语法校验
                    if not REF_PATTERN.match(tok):
                        errors.append(f"node {nid} 参数 {k}={tok!r} 格式不合法")
                        break

    return errors


def _detect_cycle(rb: Runbook) -> list[str] | None:
    """DFS 找环；返回环上节点序列（含起止重复点）或 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in rb.nodes}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for e in rb.nodes[u].edges:
            v = e.target
            if v not in color:
                continue
            if color[v] == GRAY:
                # found cycle
                idx = stack.index(v)
                return stack[idx:] + [v]
            if color[v] == WHITE:
                cyc = dfs(v)
                if cyc:
                    return cyc
        color[u] = BLACK
        stack.pop()
        return None

    for nid in rb.nodes:
        if color[nid] == WHITE:
            cyc = dfs(nid)
            if cyc:
                return cyc
    return None


# ============================================================================
# 3. 引用解析（$user.X / $nodes.id.path / $signals.type.field）
# ============================================================================

REF_PATTERN = re.compile(r"^\$(user|nodes|signals)\.[A-Za-z0-9_.\-\[\]]+$")


def _iter_refs(value: Any) -> Iterable[str]:
    """递归找到 dict/list/str 中所有 $-开头的引用字符串。"""
    if isinstance(value, str):
        if value.startswith("$"):
            yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_refs(v)


def _jsonpath_get(obj: Any, path: str) -> Any:
    """非常简版 jsonpath：只支持 ``a.b.c`` 和 ``a[0].b``。"""
    if not path:
        return obj
    cur = obj
    # 分段：把 [N] 转成 .N
    normalized = re.sub(r"\[(\d+)\]", r".\1", path)
    parts = [p for p in normalized.split(".") if p]
    for p in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            # 用 attr 访问也试一下
            cur = getattr(cur, p, None)
    return cur


def _coerce_literal(token: str) -> Any:
    """对 fallback 链尾部的字面值 token 智能转型。

    背景:runbook YAML 里 ``$user.X||1`` 整个 value 是字符串,Python 解析出来 ``"1"``
    是字符串。直接传给下游 skill 会让 ``compute_window("1")`` / ``int < "1"`` 之类
    爆 ``TypeError: '<=' not supported between instances of 'str' and 'int'``。

    转型规则:
        - ``"123"`` → int
        - ``"1.5"`` / ``"1e3"`` → float
        - ``"true"`` / ``"false"`` (大小写不敏感) → bool
        - ``"null"`` / ``"none"`` → None
        - 其它 → 原字符串
    """
    s = token.strip()
    if not s:
        return s
    low = s.lower()
    if low == "true":  return True
    if low == "false": return False
    if low in ("null", "none"): return None
    # int 先于 float 试 — "10" 走 int 而不是 float(10.0)
    try:
        # 拒绝 "10.0" 当 int(避免吃掉用户故意写的浮点)
        if "." not in s and "e" not in low:
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def resolve_ref(value: Any, ctx: "ExecutionContext") -> Any:
    """把单个值（可能含 $-引用）解析成实际值。

    支持的语法：
      ``$user.X``               用户输入
      ``$nodes.<id>.<path>``    某节点结果
      ``$signals.<type>.<f>``   第一条匹配类型的 signal
      ``$a||$b||literal``       fallback 链：第一个非 None/空 的胜出。
                                **关键场景**：``$user.pod_name||$nodes.list.pods[0].name``
                                —— 用户不传 pod_name 时回退到 list skill 的第一个 pod，
                                避免 runbook 因为 ``field_ne ... != None`` 的 if_when
                                guard 把所有诊断节点都 skip 掉。
                                **字面值自动转型**：``||1`` → ``int(1)``,``||1.5`` →
                                ``float(1.5)``,``||true`` → ``bool(True)``。否则下游
                                skill 收到字符串 ``"1"`` 做数值比较会爆 TypeError。

    非引用字符串原样返回；不识别的 $-串当作字面值返回（防止误删用户字符串）。
    """
    if not isinstance(value, str) or not value.startswith("$"):
        return value

    # ``a||b||c`` 任意一段非空就返回它；都空返回 None
    if "||" in value:
        for token in value.split("||"):
            token = token.strip()
            if not token:
                continue
            if token.startswith("$"):
                sub = resolve_ref(token, ctx)
            else:
                # 字面值兜底——做类型推断,避免下游收到字符串爆 TypeError
                sub = _coerce_literal(token)
            if sub not in (None, "", [], {}):
                return sub
        return None

    # $user.<field>
    if value.startswith("$user."):
        return _jsonpath_get(ctx.user_inputs, value[6:])

    # $nodes.<id>.<path>
    if value.startswith("$nodes."):
        rest = value[7:]
        first_dot = rest.find(".")
        if first_dot < 0:
            node_id, path = rest, ""
        else:
            node_id, path = rest[:first_dot], rest[first_dot + 1:]
        state = ctx.node_states.get(node_id)
        if not state or state.result is None:
            return None
        return _jsonpath_get(state.result, path)

    # $signals.<type>[.<field path>]
    if value.startswith("$signals."):
        rest = value[9:]
        first_dot = rest.find(".")
        if first_dot < 0:
            sig_type, path = rest, ""
        else:
            sig_type, path = rest[:first_dot], rest[first_dot + 1:]
        # 取第一条匹配 type 的 signal
        for s in ctx.collected_signals:
            if s.get("type") == sig_type:
                return _jsonpath_get(s, path) if path else s
        return None

    return value  # 未识别的 $ 串保留原文


def resolve_args(args: Any, ctx: "ExecutionContext") -> Any:
    """递归解析 args 里所有 $-引用。None 值会保留（让 skill 走默认）。"""
    if isinstance(args, dict):
        out: dict[str, Any] = {}
        for k, v in args.items():
            resolved = resolve_args(v, ctx)
            # None 不传：让平台 / skill 走默认（比如 connection_id）
            if resolved is not None:
                out[k] = resolved
        return out
    if isinstance(args, list):
        return [resolve_args(v, ctx) for v in args]
    return resolve_ref(args, ctx)


# ============================================================================
# 4. 条件评估
# ============================================================================

_SEV_ORDER = {"info": 1, "warning": 2, "critical": 3}


def evaluate_condition(cond: RunbookCondition | None, ctx: "ExecutionContext") -> bool:
    """评估一条边/节点的条件；None → True。"""
    if cond is None:
        return True

    t = cond.type

    if t == "has_signal":
        return any(s.get("type") == cond.signal_type for s in ctx.collected_signals)

    if t == "signal_severity_at_least":
        target = _SEV_ORDER.get(cond.severity or "warning", 2)
        for s in ctx.collected_signals:
            if s.get("type") == cond.signal_type and \
               _SEV_ORDER.get(s.get("severity") or "warning", 2) >= target:
                return True
        return False

    if t == "status_ok":
        st = ctx.node_states.get(cond.node or "")
        return st is not None and st.status == "done"

    if t == "status_failed":
        st = ctx.node_states.get(cond.node or "")
        return st is not None and st.status in {"error", "timeout"}

    if t in {"field_eq", "field_ne", "field_contains", "field_gt", "field_lt"}:
        actual = resolve_ref(cond.path or "", ctx)
        expected = cond.value
        if t == "field_eq": return actual == expected
        if t == "field_ne": return actual != expected
        if t == "field_contains":
            if actual is None: return False
            try:
                return str(expected) in str(actual) if not isinstance(actual, list) \
                    else expected in actual
            except (TypeError, ValueError) as exc:
                # 走到这里通常是 actual / expected 不是预期类型——log 一下，
                # 让 runbook 作者能定位为啥这条 condition 永远 False。
                logger.warning(
                    "condition field_contains 评估失败：path=%s actual=%r expected=%r → %s",
                    cond.path, actual, expected, exc,
                )
                return False
        if t == "field_gt":
            try: return float(actual) > float(expected)
            except (TypeError, ValueError): return False
        if t == "field_lt":
            try: return float(actual) < float(expected)
            except (TypeError, ValueError): return False

    if t == "any_of":
        return any(evaluate_condition(c, ctx) for c in cond.conditions)
    if t == "all_of":
        return all(evaluate_condition(c, ctx) for c in cond.conditions)
    if t == "not":
        # 注意：``cond.conditions and not ...`` 会在 conditions 为空时返回空列表，
        # 上游期望 bool；显式转 bool 避免下游 ``if condition:`` 读到 [] 误判。
        if not cond.conditions:
            logger.warning("condition 'not' 缺少 inner conditions，按 false 处理")
            return False
        return not evaluate_condition(cond.conditions[0], ctx)

    logger.warning("未知条件类型：%s，按 false 处理", t)
    return False


# ============================================================================
# 5. 执行上下文 + 节点状态
# ============================================================================

@dataclass
class NodeState:
    node_id: str
    status: str = "pending"          # pending | running | done | error | timeout | skipped | cancelled
    started_at: float | None = None
    ended_at: float | None = None
    latency_ms: int | None = None
    args_resolved: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    envelope: dict[str, Any] | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    attempts: int = 0
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class ExecutionContext:
    runbook: Runbook
    user_inputs: dict[str, Any] = field(default_factory=dict)
    node_states: dict[str, NodeState] = field(default_factory=dict)
    collected_signals: list[dict[str, Any]] = field(default_factory=list)
    seen_signal_keys: set[tuple] = field(default_factory=set)
    global_status: str = "running"   # running | done | timeout | failed | cancelled
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    execution_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    abort_reason: str | None = None

    def add_signal(self, sig: dict[str, Any]) -> bool:
        """去重后添加到全局 signals；返回是否新增。"""
        key = (
            sig.get("type"),
            sig.get("next_skill"),
            tuple(sorted((k, str(v)) for k, v in (sig.get("next_args") or {}).items())),
        )
        if key in self.seen_signal_keys:
            return False
        self.seen_signal_keys.add(key)
        self.collected_signals.append(sig)
        return True


@dataclass
class ExecutionResult:
    """RunbookExecutor.execute() 的返回。"""

    execution_id: str
    runbook_key: str
    runbook_version: int
    global_status: str
    started_at: str
    ended_at: str
    total_ms: int
    node_states: list[dict[str, Any]]
    all_signals: list[dict[str, Any]]
    abort_reason: str | None = None

    def to_skill_result(self) -> dict[str, Any]:
        """供 ``platform_run_runbook`` 这个 skill 返回；模型从这里读所有证据。"""
        return {
            "execution_id": self.execution_id,
            "runbook_key": self.runbook_key,
            "runbook_version": self.runbook_version,
            "global_status": self.global_status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "total_ms": self.total_ms,
            "node_count": len(self.node_states),
            "node_states": self.node_states,
            "_signals": self.all_signals,   # 让 agent 的 signal injection 仍然能拿到
            "abort_reason": self.abort_reason,
        }


# ============================================================================
# 6. RunbookExecutor
# ============================================================================

class RunbookExecutor:
    """图执行器。

    BFS + visited 集合保证不重入。每节点单线程跑、整体串行；并行执行可后续加。
    """

    def __init__(self, runtime, *, executor_pool: ThreadPoolExecutor | None = None) -> None:
        self.runtime = runtime
        # 共享一个线程池只用来给单 skill 加 timeout
        self._pool = executor_pool or ThreadPoolExecutor(max_workers=4, thread_name_prefix="runbook-skill")

    def execute(
        self,
        runbook: Runbook,
        *,
        user_inputs: dict[str, Any],
        skill_ctx,
    ) -> ExecutionResult:
        # 入参检查
        for required in runbook.inputs:
            if required not in (user_inputs or {}):
                ctx = ExecutionContext(
                    runbook=runbook, user_inputs=user_inputs or {},
                    global_status="failed",
                    abort_reason=f"缺少必填输入 ``{required}``",
                )
                return self._finalize(ctx)

        ctx = ExecutionContext(runbook=runbook, user_inputs=user_inputs or {})
        deadline = ctx.started_at + runbook.max_total_seconds
        visited: set[str] = set()
        queue: list[str] = [runbook.start_node]

        logger.info("runbook 开始执行 [%s] %s, inputs=%s",
                    ctx.execution_id[:8], runbook.key, list(user_inputs.keys()))

        while queue:
            if time.time() > deadline:
                ctx.global_status = "timeout"
                ctx.abort_reason = f"超过 max_total_seconds={runbook.max_total_seconds}"
                logger.warning("runbook [%s] 全局超时", ctx.execution_id[:8])
                break

            node_id = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)

            node = runbook.nodes.get(node_id)
            if not node:
                logger.warning("runbook [%s] 路过不存在的 node id=%s（已被验证器拦下不应到这）",
                               ctx.execution_id[:8], node_id)
                continue

            # if_when 守卫
            if not evaluate_condition(node.if_when, ctx):
                state = NodeState(
                    node_id=node_id, status="skipped",
                    skipped_reason="if_when condition not satisfied",
                )
                ctx.node_states[node_id] = state
                self._enqueue_outgoing(node, ctx, queue)
                continue

            self._execute_node(node, ctx, skill_ctx)
            state = ctx.node_states[node_id]

            if state.status in {"error", "timeout"} and node.on_error == "fail":
                ctx.global_status = "failed"
                ctx.abort_reason = f"node {node_id} {state.status}：{state.error}"
                logger.warning("runbook [%s] 因 node %s 失败而终止", ctx.execution_id[:8], node_id)
                break
            # skip / continue 都继续走边
            self._enqueue_outgoing(node, ctx, queue)

        if ctx.global_status == "running":
            ctx.global_status = "done"
        return self._finalize(ctx)

    # -------- 内部 --------

    def _execute_node(self, node: RunbookNode, ctx: ExecutionContext, skill_ctx) -> None:
        state = NodeState(node_id=node.id, status="running", started_at=time.time())
        ctx.node_states[node.id] = state

        try:
            args = resolve_args(node.args, ctx) or {}
        except Exception as exc:
            state.status = "error"
            state.error = f"参数解析失败：{exc}"
            state.ended_at = time.time()
            state.latency_ms = int((state.ended_at - state.started_at) * 1000)
            return
        state.args_resolved = args

        attempt = 0
        while True:
            state.attempts = attempt + 1
            try:
                envelope = self._invoke_with_timeout(node.skill, args, skill_ctx, node.timeout_seconds)
            except FutTimeoutError:
                state.status = "timeout"
                state.error = f"skill {node.skill} 在 {node.timeout_seconds}s 内未返回"
                if attempt < node.retry:
                    attempt += 1
                    continue
                break
            except Exception as exc:
                state.status = "error"
                state.error = f"调用失败：{exc}"
                logger.exception("runbook [%s] node %s 异常", ctx.execution_id[:8], node.id)
                if attempt < node.retry:
                    attempt += 1
                    continue
                break

            state.envelope = envelope
            state.result = envelope.get("result")
            sigs = collect_signals(envelope)
            state.signals = sigs
            for s in sigs:
                ctx.add_signal(s)

            env_status = envelope.get("status")
            if env_status == "ok":
                state.status = "done"
                break
            if env_status == "needs_confirmation":
                # 写操作进了 runbook 不该走到这——验证器会拦；保险起见这里也直接失败
                state.status = "error"
                state.error = "writeskill leaked into runbook (needs_confirmation)"
                break
            # error / 其它
            state.status = "error"
            state.error = (envelope.get("message") or
                           (envelope.get("result") or {}).get("error") or
                           envelope.get("error_code") or "unknown error")
            if attempt < node.retry:
                attempt += 1
                continue
            break

        state.ended_at = time.time()
        state.latency_ms = int((state.ended_at - (state.started_at or state.ended_at)) * 1000)

    def _invoke_with_timeout(self, skill_code: str, args: dict, skill_ctx, timeout: int) -> dict:
        future = self._pool.submit(self.runtime.skill_invoker.invoke, skill_code, args, skill_ctx)
        try:
            return future.result(timeout=max(1, int(timeout)))
        except FutTimeoutError:
            future.cancel()
            raise

    def _enqueue_outgoing(self, node: RunbookNode, ctx: ExecutionContext, queue: list[str]) -> None:
        for edge in node.edges:
            if not evaluate_condition(edge.when, ctx):
                continue
            if edge.target not in ctx.node_states and edge.target not in queue:
                queue.append(edge.target)

    def _finalize(self, ctx: ExecutionContext) -> ExecutionResult:
        ctx.ended_at = time.time()
        # 把没跑过的节点也写一条 pending 状态进结果，方便审计
        for nid in ctx.runbook.nodes:
            if nid not in ctx.node_states:
                ctx.node_states[nid] = NodeState(
                    node_id=nid, status="pending",
                    skipped_reason="未到达（被边条件挡住或上游失败）",
                )
        from datetime import UTC, datetime

        def iso(t: float | None) -> str:
            return datetime.fromtimestamp(t, UTC).isoformat() if t else ""

        return ExecutionResult(
            execution_id=ctx.execution_id,
            runbook_key=ctx.runbook.key,
            runbook_version=ctx.runbook.version,
            global_status=ctx.global_status,
            started_at=iso(ctx.started_at),
            ended_at=iso(ctx.ended_at),
            total_ms=int(((ctx.ended_at or time.time()) - ctx.started_at) * 1000),
            node_states=[ns.to_dict() for ns in ctx.node_states.values()],
            all_signals=list(ctx.collected_signals),
            abort_reason=ctx.abort_reason,
        )


# ============================================================================
# 7. RunbookRegistry — 内存注册表，从 store 加载，支持热刷新
# ============================================================================

class RunbookRegistry:
    """运行时持有的 runbook 集合；启动时从 DB 拉，admin 改动时调 reload()。"""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()
        self._runbooks: dict[str, Runbook] = {}

    # ---- 加载 ----

    def reload(self) -> dict[str, list[str]]:
        """从 store 重新加载所有 runbook；返回每个 key → 错误列表。"""
        store = self.runtime.store
        if not hasattr(store, "list_runbooks"):
            return {}
        records = store.list_runbooks()
        known_skills = {s.code for s in self.runtime.skill_registry.list(only_enabled=False)}
        write_skills = {s.code for s in self.runtime.skill_registry.list(only_enabled=False) if not s.read_only}

        new_map: dict[str, Runbook] = {}
        errors: dict[str, list[str]] = {}
        for r in records:
            try:
                rb = load_runbook_from_dict(r["definition"])
                rb.version = int(r.get("version") or rb.version)
                rb.enabled = bool(r.get("enabled", rb.enabled))
            except RunbookLoadError as e:
                errors[r.get("key", "<unknown>")] = [str(e)]
                continue
            errs = validate_runbook(rb, known_skills=known_skills, write_skills=write_skills)
            if errs:
                errors[rb.key] = errs
                continue
            new_map[rb.key] = rb

        with self._lock:
            self._runbooks = new_map
        logger.info("runbook 注册表已加载：%d 个有效，%d 个错误",
                    len(new_map), len(errors))
        return errors

    def list_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._runbooks.keys())

    def list_runbooks(self) -> list[Runbook]:
        with self._lock:
            return list(self._runbooks.values())

    def get(self, key: str) -> Runbook | None:
        with self._lock:
            return self._runbooks.get(key)

    # ---- 触发匹配 ----

    def match_by_query(self, user_query: str) -> Runbook | None:
        """根据用户原话 + 每个 runbook 的 triggers 关键词做匹配，命中最多的赢。"""
        cands = self.match_all_by_query(user_query)
        return cands[0] if cands else None

    def match_all_by_query(self, user_query: str) -> list[Runbook]:
        """返回**所有**命中的 runbook,按命中关键词数降序(平票按 key 字典序 deterministic)。

        给预路由按"集群类型兼容性"二次筛选用——同一个"集群巡检"通用词可能同时命中
        swarm 巡检和 k8s 巡检两个 runbook,调用方据用户路由到的集群类型挑兼容那个。
        """
        if not user_query:
            return []
        q = user_query.lower()
        scored: list[tuple[int, str, Runbook]] = []
        with self._lock:
            for rb in self._runbooks.values():
                if not rb.enabled:
                    continue
                hits = sum(1 for t in rb.triggers if t and t.lower() in q)
                if hits:
                    scored.append((hits, rb.key, rb))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [rb for _, _, rb in scored]

    def find(self, name: str | None, user_query: str = "") -> Runbook | None:
        if name:
            rb = self.get(name)
            if rb and rb.enabled:
                return rb
        return self.match_by_query(user_query)


# ============================================================================
# 8. 默认报告生成 prompt（runbook 没自定义时用这个）
# ============================================================================

DEFAULT_REPORT_PROMPT = """\
你是 AI 运维助手。下面是一个诊断剧本（runbook）的完整执行结果——平台已经按图自动取证。
请用中文五段式报告回复用户：

**当前状态**：服务/Pod/主机现在是健康还是异常，关键数字。
**检测过程**：剧本按什么顺序查了哪些 skill，跨域 pivot 在哪一步发生。
**关键证据**：1–3 条最核心的原始信息，用 ``code`` 块引用日志/错误码原文。
**判断结论**：根因是什么；不能 100% 确认就写"高度疑似 + 备选"。
**建议操作**：具体可执行——重启 / 扩容 / 清理。如果建议是写操作，
明确告诉用户「我可以帮你执行 ``swarm_xxx``，请下方点击确认」。

⚠️ **诊断完整性硬性要求**——查报告里 ``node_states`` 找：
- 有 ``status == "error"`` 或 ``status == "timeout"`` 的节点 → 在"检测过程"段
  **明确写出哪一步失败、失败原因 (state.error)**，不要绕过；
- 全局 ``global_status != "done"`` 或 ``abort_reason`` 非空 → 在"判断结论"段
  开头第一句声明"⚠️ 本次诊断中途中止：<原因>，证据可能不完整"。
- 关键 pivot 节点（zabbix_get_host_overview / host_storage_overview /
  host_kernel_events 等）失败时不能只贴前置节点的证据就下结论——必须告诉用户
  缺了什么信息、补什么 skill 能补全。

请只引用 node_states 里 status=done 节点的真实证据，不要编造。
执行中已经有 _signals 数组，是各节点主动发出的结构化信号——这些信号的 evidence 字段
就是你写"关键证据"段时最优先要引用的内容。
"""


def render_final_report(
    result: ExecutionResult,
    *,
    user_message: str,
    model_client,
    custom_prompt: str = "",
) -> str:
    """让模型基于 ExecutionResult 写最终中文报告。"""
    prompt = custom_prompt or DEFAULT_REPORT_PROMPT
    payload = {
        "user_message": user_message,
        "runbook": result.runbook_key,
        "global_status": result.global_status,
        "abort_reason": result.abort_reason,
        "duration_ms": result.total_ms,
        "node_states": result.node_states,
        "all_signals": result.all_signals,
    }
    msg = model_client.create_completion(messages=[
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2, default=str)},
    ])
    return msg.get("content", "") or "（模型未输出报告）"
