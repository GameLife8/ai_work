"""HTTP Skill 引擎：让 admin 用 YAML 声明 HTTP API 端点 → 自动变成平台 skill。

核心思路
--------
**不再写 Python 代码**。每个外部系统的每个 endpoint 写一份 YAML 落到 ``platform_http_skill``
表，平台启动 / admin 编辑后调用 ``HttpSkillLoader.reload()``，把每条 YAML
注册成 ``SkillSpec`` 进 ``SkillRegistry``——和原生 Python skill 共用同一份执行链路、
审计、二次确认、信号机制、MCP 暴露、runbook 引用。

YAML 字段速查（详见 [docs/http-skill.md](../docs/http-skill.md)）
::

    code: jira_search_issues          # 全局唯一
    name: 在 Jira 里搜 issue
    description: 何时使用 + 信号→下一步
    category: ticketing
    connection_id: jira-prod          # 绑定 http_api connection
    read_only: true
    visibility: all
    requires_admin_approval: false

    method: POST
    path: /rest/api/3/search
    query_template:                    # GET 参数（可选）
      page: "{{ page | default(1) }}"
    headers_template:                  # 调用级 header（可选）
      X-Request-ID: "{{ uuid() }}"
    body_template: |                   # POST/PUT/PATCH 用（Jinja2 沙箱）
      { "jql": "{{ jql }}", "maxResults": {{ limit | default(20) }} }

    params_schema: { ... JSON Schema ... }

    response_extract:                  # 精简 response，只给模型看关键字段
      total: '$.total'
      issue_keys: '$.issues[*].key'

    signal_rules:                      # 把 HTTP 响应转成结构化信号
      - when: { type: http_status, eq: 401 }
        emit: { type: auth_failure, severity: critical, evidence: ... }

    timeout_seconds: 30
    retry: 0

模板沙箱
--------
Jinja2 ``SandboxedEnvironment``，只暴露安全的内置 + ``tojson`` / ``urlencode`` /
``now()`` / ``uuid()`` / ``default()``。模型给的 params 不会被解释为 Jinja 代码
（只走 Schema 校验后塞进 context dict）。

Response Extract DSL
--------------------
- ``$.foo.bar``        → 取嵌套字段
- ``$.foo[0].name``    → 数组下标
- ``$.foo[*].name``    → ``[*]`` 后面接子路径，返回数组（最常用）
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid as _uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from jinja2.sandbox import SandboxedEnvironment

from ops_platform.signals import attach as attach_signals


logger = logging.getLogger(__name__)


# ============================================================================
# 1. Spec 数据类
# ============================================================================

@dataclass
class HttpSignalRule:
    when: dict[str, Any]    # {type: http_status|response_field|response_text, ...}
    emit: dict[str, Any]    # {type, severity, evidence, next_skill?, next_args?, context?}


@dataclass
class HttpSkillSpec:
    code: str
    name: str
    description: str
    method: str
    path: str
    params_schema: dict[str, Any]
    category: str = "integration"
    connection_id: str | None = None       # 默认 http_api connection；可被 skill 调用时 override
    read_only: bool = True
    requires_admin_approval: bool = False
    visibility: str = "all"
    confirmation_ttl_seconds: int = 300
    enabled: bool = True
    query_template: dict[str, Any] = field(default_factory=dict)
    headers_template: dict[str, Any] = field(default_factory=dict)
    body_template: str = ""                 # 字符串：Jinja2 渲染后期望是合法 JSON
    response_extract: dict[str, str] = field(default_factory=dict)
    signal_rules: list[HttpSignalRule] = field(default_factory=list)
    timeout_seconds: int = 30
    retry: int = 0


class HttpSkillLoadError(Exception):
    """YAML/dict 解析失败。"""


VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def parse_http_skill(definition: dict[str, Any]) -> HttpSkillSpec:
    """从 dict（YAML 解析后）构造 HttpSkillSpec。校验关键字段。"""
    if not isinstance(definition, dict):
        raise HttpSkillLoadError("definition 必须是 dict")
    code = (definition.get("code") or "").strip()
    if not code:
        raise HttpSkillLoadError("缺少 code")
    if not re.match(r"^[a-z][a-z0-9_]+$", code):
        raise HttpSkillLoadError(f"code={code!r} 不合法（要求小写字母+数字+下划线，字母开头）")
    method = str(definition.get("method") or "GET").upper()
    if method not in VALID_METHODS:
        raise HttpSkillLoadError(f"method={method} 不合法（{sorted(VALID_METHODS)}）")
    path = definition.get("path")
    if not path:
        raise HttpSkillLoadError(f"{code}: 缺少 path")

    body_t = definition.get("body_template") or ""
    if isinstance(body_t, (dict, list)):
        # 友好处理：admin 直接写 dict 也认（自动 JSON 序列化）
        body_t = json.dumps(body_t, ensure_ascii=False, indent=2)

    rules: list[HttpSignalRule] = []
    for r in definition.get("signal_rules") or []:
        if not isinstance(r, dict) or "when" not in r or "emit" not in r:
            raise HttpSkillLoadError(f"{code}: signal_rules 元素必须含 when 和 emit")
        rules.append(HttpSignalRule(when=dict(r["when"]), emit=dict(r["emit"])))

    spec = HttpSkillSpec(
        code=code,
        name=definition.get("name") or code,
        description=definition.get("description") or "（未填描述）",
        category=definition.get("category") or "integration",
        connection_id=definition.get("connection_id"),
        read_only=bool(definition.get("read_only", True)),
        requires_admin_approval=bool(definition.get("requires_admin_approval", False)),
        visibility=definition.get("visibility") or "all",
        confirmation_ttl_seconds=int(definition.get("confirmation_ttl_seconds") or 300),
        enabled=bool(definition.get("enabled", True)),
        method=method,
        path=path,
        params_schema=definition.get("params_schema") or {"type": "object", "properties": {}},
        query_template=dict(definition.get("query_template") or {}),
        headers_template=dict(definition.get("headers_template") or {}),
        body_template=body_t,
        response_extract=dict(definition.get("response_extract") or {}),
        signal_rules=rules,
        timeout_seconds=int(definition.get("timeout_seconds") or 30),
        retry=int(definition.get("retry") or 0),
    )

    # 写操作没标 admin 审批 → warning 但不阻塞
    if not spec.read_only and not spec.requires_admin_approval:
        logger.warning(
            "HTTP skill %s 是写操作但没开 requires_admin_approval；"
            "高危外部系统建议打开。",
            spec.code,
        )

    # Jinja2 模板提前编译一遍，发现语法错就直接拒绝
    env = _build_jinja_env()
    for k, t in {
        "path": path,
        **{f"query.{k}": v for k, v in spec.query_template.items() if isinstance(v, str)},
        **{f"headers.{k}": v for k, v in spec.headers_template.items() if isinstance(v, str)},
        "body": spec.body_template,
    }.items():
        if not isinstance(t, str) or not t:
            continue
        try:
            env.from_string(t)
        except Exception as exc:
            raise HttpSkillLoadError(f"{code} 模板 {k} 解析失败：{exc}") from exc

    return spec


# ============================================================================
# 2. Jinja2 沙箱 + 自定义函数
# ============================================================================

def _build_jinja_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(autoescape=False, keep_trailing_newline=True)
    env.globals["now"] = lambda: datetime.now(UTC).isoformat()
    env.globals["uuid"] = lambda: _uuid.uuid4().hex
    env.globals["timestamp"] = lambda: int(time.time())

    # 安全的 url quote 过滤器
    from urllib.parse import quote as _quote
    env.filters["urlencode"] = lambda s: _quote(str(s), safe="")
    env.filters["url_quote"] = env.filters["urlencode"]

    return env


_JINJA = _build_jinja_env()


def render_string(template: str, variables: dict[str, Any]) -> str:
    if not template:
        return template
    return _JINJA.from_string(template).render(**variables)


def render_dict(d: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            try:
                out[k] = render_string(v, variables)
            except Exception as exc:
                raise RuntimeError(f"模板字段 {k} 渲染失败：{exc}") from exc
        else:
            out[k] = v
    return out


# ============================================================================
# 3. JSONPath（带 [*] 数组展开）
# ============================================================================

_PATH_TOKEN = re.compile(r"\[(\d+|\*)\]")


def _json_query(obj: Any, path: str) -> Any:
    """支持 ``$.a.b.c`` / ``$.a[0].b`` / ``$.a[*].b`` 三种形态。

    ``[*]`` 出现时返回 list（对每个元素继续走剩余路径），其它返回原值或 None。
    """
    if not path:
        return obj
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    p = p.lstrip(".")
    if not p:
        return obj
    # 把 [N] / [*] 转成 .N / .*  让 split 简单
    p = _PATH_TOKEN.sub(lambda m: f".{m.group(1)}", p)
    parts = [x for x in p.split(".") if x != ""]
    return _walk(obj, parts)


def _walk(cur: Any, parts: list[str]) -> Any:
    if not parts:
        return cur
    head, rest = parts[0], parts[1:]

    if head == "*":
        if not isinstance(cur, list):
            return None
        return [_walk(x, rest) for x in cur]

    if isinstance(cur, list):
        try:
            idx = int(head)
        except ValueError:
            return None
        if idx >= len(cur) or idx < -len(cur):
            return None
        return _walk(cur[idx], rest)

    if isinstance(cur, dict):
        return _walk(cur.get(head), rest)

    return None


def apply_response_extract(extract: dict[str, str], response_obj: Any) -> dict[str, Any]:
    """按 extract 配置精简响应；找不到的字段返回 None。"""
    out: dict[str, Any] = {}
    for out_key, expr in (extract or {}).items():
        try:
            out[out_key] = _json_query(response_obj, expr)
        except Exception as exc:
            out[out_key] = None
            logger.warning("response_extract %s=%s 失败：%s", out_key, expr, exc)
    return out


# ============================================================================
# 4. Signal Rules 评估
# ============================================================================

def _signal_when_matches(when: dict[str, Any], status: int, response_obj: Any, response_text: str) -> bool:
    t = when.get("type")
    if t == "http_status":
        if "eq" in when: return status == int(when["eq"])
        if "ne" in when: return status != int(when["ne"])
        if "gte" in when: return status >= int(when["gte"])
        if "lte" in when: return status <= int(when["lte"])
        if "in" in when:  return status in (when["in"] or [])
        return False
    if t == "response_field":
        path = when.get("path") or ""
        actual = _json_query(response_obj, path) if response_obj is not None else None
        if "eq" in when: return actual == when["eq"]
        if "ne" in when: return actual != when["ne"]
        if "contains" in when:
            try:
                return when["contains"] in (actual or [])
            except TypeError:
                return str(when["contains"]) in str(actual or "")
        if "gt" in when:
            try: return float(actual) > float(when["gt"])
            except (TypeError, ValueError): return False
        if "lt" in when:
            try: return float(actual) < float(when["lt"])
            except (TypeError, ValueError): return False
        if "exists" in when:
            return (actual is not None) is bool(when["exists"])
        return False
    if t == "response_text":
        if "contains" in when:
            return when["contains"] in (response_text or "")
        return False
    return False


def _render_emit(emit: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    """signal 的 evidence / context / next_args 也支持 Jinja2，能引用 response/params。"""
    out: dict[str, Any] = {}
    for k, v in emit.items():
        if isinstance(v, str):
            try:
                out[k] = render_string(v, variables)
            except Exception:
                out[k] = v
        elif isinstance(v, dict):
            out[k] = render_dict(v, variables)
        else:
            out[k] = v
    out.setdefault("severity", "warning")
    return out


def evaluate_signal_rules(
    rules: list[HttpSignalRule],
    *,
    status: int,
    response_obj: Any,
    response_text: str,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    if not rules:
        return []
    variables = {"response": response_obj or {}, "params": params or {}, "status": status}
    out: list[dict[str, Any]] = []
    for rule in rules:
        if _signal_when_matches(rule.when, status, response_obj, response_text):
            out.append(_render_emit(rule.emit, variables))
    return out


# ============================================================================
# 5. Runner
# ============================================================================

class HttpSkillRunner:
    """实际执行一个 HTTP skill 的 callable。

    每个 HTTP skill 注册到 SkillRegistry 时，handler 是一个闭包：
        ``lambda ctx, **params: HttpSkillRunner(runtime).run(spec, params, ctx)``

    这样：
        - tool list 暴露 spec.params_schema（模型看到的工具）
        - invoker 跑 handler → 走我们这边的真实 HTTP 调用
        - read_only / requires_admin_approval 已经在 SkillSpec 上，二次确认链原生支持
    """

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def run(self, spec: HttpSkillSpec, params: dict[str, Any], ctx) -> dict[str, Any]:
        # 解析 connection（params.connection_id > spec.connection_id > 平台默认）
        conn_id = params.get("connection_id") or spec.connection_id
        try:
            client = (
                self.runtime.connection_manager.get_client(conn_id)
                if conn_id
                else ctx.connection_for("http_api")
            )
        except Exception as exc:
            return {
                "error": "connection_resolve_failed",
                "message": f"无法解析 http_api 连接：{exc}",
                "spec_code": spec.code,
            }

        # 渲染模板
        variables = {"params": params, **params}    # 兼容 {{ params.x }} 和 {{ x }} 两种写法
        try:
            url_path = render_string(spec.path, variables)
            query = render_dict(spec.query_template, variables) if spec.query_template else None
            headers = render_dict(spec.headers_template, variables) if spec.headers_template else None
            body_obj = None
            if spec.body_template:
                body_text = render_string(spec.body_template, variables)
                body_text = body_text.strip()
                if body_text:
                    try:
                        body_obj = json.loads(body_text)
                    except json.JSONDecodeError as exc:
                        return {
                            "error": "body_render_invalid_json",
                            "message": f"body 模板渲染后不是合法 JSON：{exc}",
                            "rendered": body_text[:500],
                        }
        except Exception as exc:
            return {
                "error": "template_render_failed",
                "message": str(exc),
            }

        # retry 包装
        last: dict[str, Any] | None = None
        for attempt in range(spec.retry + 1):
            http_result = client.request(
                spec.method, url_path,
                query=query, headers=headers, json_body=body_obj,
                timeout=spec.timeout_seconds,
            )
            ok = http_result.ok or 200 <= http_result.status < 500
            if ok or attempt == spec.retry:
                last = http_result.to_dict()
                break

        # 应用 response_extract
        if spec.response_extract:
            extracted = apply_response_extract(spec.response_extract, http_result.json_body)
            last["extracted"] = extracted

        # 应用 signal_rules
        signals = evaluate_signal_rules(
            spec.signal_rules,
            status=http_result.status,
            response_obj=http_result.json_body,
            response_text=http_result.text_body or "",
            params=params,
        )
        if signals:
            attach_signals(last, signals)

        return last
