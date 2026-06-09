from __future__ import annotations

import functools
import logging
from typing import Any

from flask import Blueprint, current_app, g, jsonify, request

from ops_platform import drivers as driver_registry
from ops_platform.auth import hash_password, issue_token, verify_password, verify_token


logger = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin/api/v1")


# ---------- helpers ----------

def _runtime():
    return current_app.extensions["runtime"]


def _store():
    return _runtime().store


def _jwt_secret() -> str:
    return current_app.config.get("ADMIN_JWT_SECRET") or "change-me-in-prod"


def _bearer_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _login_required(role: str | None = None):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            token = _bearer_token()
            if not token:
                return jsonify({"error": "unauthorized"}), 401
            payload = verify_token(token, _jwt_secret())
            if not payload:
                return jsonify({"error": "invalid_token"}), 401
            user = _store().get_user(payload.get("sub", ""))
            if not user or not user.get("enabled", True):
                return jsonify({"error": "user_disabled"}), 401
            if role and user.get("role") != role:
                return jsonify({"error": "forbidden"}), 403
            g.current_user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator


MASK = "****"
CONNECTION_SECRETS = ("password", "api_key", "token", "secret", "secret_key")


def _strip_secrets(record: dict, keys: tuple[str, ...]) -> dict:
    """脱敏返回（admin 也只看见 ****，要看明文调单独的解密接口；当前版本简单处理）。"""
    out = dict(record)
    cfg = out.get("config")
    if isinstance(cfg, dict):
        out["config"] = {k: (MASK if k in keys and v else v) for k, v in cfg.items()}
    for k in keys:
        if k in out and out[k]:
            out[k] = MASK
    return out


def _unmask_config(new_config: dict, existing_config: dict, keys: tuple[str, ...]) -> dict:
    """前端 PATCH 回填时，敏感字段如果还是 **** 或空串，回退成库里的真实值。"""
    if not isinstance(new_config, dict):
        return new_config
    merged = dict(new_config)
    for k in keys:
        v = merged.get(k)
        if v in (None, "", MASK):
            if existing_config and existing_config.get(k):
                merged[k] = existing_config[k]
            elif v == MASK:
                # 无 existing 兜底（不应发生），别把 **** 当真值落库
                merged.pop(k, None)
    return merged


def _unmask_value(new_value, existing_value):
    if new_value in (None, "", MASK):
        return existing_value
    return new_value


# ---------- auth ----------

@admin_bp.post("/auth/login")
def login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"error": "missing_credentials"}), 400
    user = _store().get_user_by_username(username)
    if not user or not user.get("enabled", True):
        return jsonify({"error": "invalid_credentials"}), 401
    if not verify_password(password, user["password_hash"]):
        return jsonify({"error": "invalid_credentials"}), 401
    token = issue_token({"sub": user["id"], "role": user["role"]}, _jwt_secret())
    return jsonify({
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "display_name": user.get("display_name"),
        },
    })


@admin_bp.get("/auth/me")
@_login_required()
def me():
    user = g.current_user
    return jsonify({
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "display_name": user.get("display_name"),
    })


# ---------- users (admin only) ----------

@admin_bp.get("/users")
@_login_required("admin")
def list_users():
    users = _store().list_users()
    return jsonify([
        {k: v for k, v in u.items() if k != "password_hash"} for u in users
    ])


@admin_bp.post("/users")
@_login_required("admin")
def create_user():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role", "user")
    display_name = body.get("display_name", "")
    if not username or not password:
        return jsonify({"error": "missing_fields"}), 400
    try:
        record = _store().create_user(
            username=username,
            password_hash=hash_password(password),
            role=role,
            display_name=display_name,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    record.pop("password_hash", None)
    return jsonify(record), 201


@admin_bp.patch("/users/<user_id>")
@_login_required("admin")
def update_user(user_id):
    body = request.get_json(silent=True) or {}
    fields: dict[str, Any] = {}
    for k in ("role", "display_name", "enabled"):
        if k in body:
            fields[k] = body[k]
    if "password" in body and body["password"]:
        fields["password_hash"] = hash_password(body["password"])
    record = _store().update_user(user_id, **fields)
    record.pop("password_hash", None)
    return jsonify(record)


@admin_bp.delete("/users/<user_id>")
@_login_required("admin")
def delete_user(user_id):
    _store().delete_user(user_id)
    return ("", 204)


# ---------- driver schemas ----------

@admin_bp.get("/drivers")
@_login_required("admin")
def list_drivers():
    return jsonify([d.schema() for d in driver_registry.all_drivers()])


# ---------- connections ----------

@admin_bp.get("/connections")
@_login_required("admin")
def list_connections():
    type_code = request.args.get("type")
    items = _store().list_connections(type_code=type_code)
    return jsonify([_strip_secrets(c, CONNECTION_SECRETS) for c in items])


@admin_bp.get("/connections/<connection_id>")
@_login_required("admin")
def get_connection(connection_id):
    record = _store().get_connection(connection_id)
    if not record:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_strip_secrets(record, CONNECTION_SECRETS))


@admin_bp.post("/connections")
@_login_required("admin")
def create_connection():
    body = request.get_json(silent=True) or {}
    try:
        record = _runtime().connection_manager.create(
            type_code=body["type_code"],
            name=body["name"],
            alias=body.get("alias", ""),
            config=body.get("config") or {},
            is_default=bool(body.get("is_default")),
            created_by=g.current_user["username"],
            tags=body.get("tags") or [],
        )
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_strip_secrets(record, CONNECTION_SECRETS)), 201


@admin_bp.patch("/connections/<connection_id>")
@_login_required("admin")
def update_connection(connection_id):
    body = request.get_json(silent=True) or {}
    fields = {k: v for k, v in body.items() if k in {"name", "alias", "config", "tags", "is_default", "enabled"}}
    if "config" in fields:
        existing = _store().get_connection(connection_id) or {}
        fields["config"] = _unmask_config(fields["config"], existing.get("config") or {}, CONNECTION_SECRETS)
    record = _runtime().connection_manager.update(connection_id, **fields)

    # 如果改的是默认 connection（zabbix/swarm），让 alert pipeline 老链路也立即用新值
    if record and (record.get("is_default") or fields.get("is_default")):
        refresh = getattr(_runtime(), "refresh_legacy_clients", None)
        if callable(refresh):
            refresh()

    return jsonify(_strip_secrets(record, CONNECTION_SECRETS))


@admin_bp.delete("/connections/<connection_id>")
@_login_required("admin")
def delete_connection(connection_id):
    _runtime().connection_manager.delete(connection_id)
    return ("", 204)


@admin_bp.post("/connections/<connection_id>/validate")
@_login_required("admin")
def validate_connection(connection_id):
    """跑一次 driver.validate；同时把结果（ok / fail）写回 connection.status，
    让接入列表的"状态"列从 unknown 变成实时的健康状态。"""
    result = _runtime().connection_manager.validate(connection_id)
    new_status = "ok" if result.get("ok") else "fail"
    try:
        _runtime().connection_manager.update(connection_id, status=new_status)
    except Exception as exc:  # pragma: no cover
        logger.warning("连通后回写 status 失败：%s", exc)
    result["persisted_status"] = new_status
    return jsonify(result)


@admin_bp.post("/connections/validate")
@_login_required("admin")
def validate_config():
    body = request.get_json(silent=True) or {}
    config = body.get("config") or {}
    connection_id = body.get("connection_id")
    if connection_id:
        existing = _store().get_connection(connection_id) or {}
        config = _unmask_config(config, existing.get("config") or {}, CONNECTION_SECRETS)
    return jsonify(_runtime().connection_manager.validate_config(
        body.get("type_code"), config,
    ))


# ---------- models ----------

@admin_bp.get("/models")
@_login_required("admin")
def list_models():
    items = _store().list_model_configs()
    return jsonify([_strip_secrets(m, ("api_key",)) for m in items])


@admin_bp.post("/models")
@_login_required("admin")
def create_model():
    body = request.get_json(silent=True) or {}
    record = _runtime().model_manager.create(
        provider=body["provider"],
        name=body["name"],
        base_url=body["base_url"],
        api_key=body.get("api_key", ""),
        model=body.get("model", ""),
        is_default=bool(body.get("is_default")),
        created_by=g.current_user["username"],
    )
    return jsonify(_strip_secrets(record, ("api_key",))), 201


@admin_bp.patch("/models/<model_id>")
@_login_required("admin")
def update_model(model_id):
    body = request.get_json(silent=True) or {}
    fields = {k: v for k, v in body.items()
              if k in {"provider", "name", "base_url", "api_key", "model",
                       "timeout_seconds", "is_default", "enabled",
                       "tool_choice_preference"}}
    if "api_key" in fields:
        existing = _store().get_model_config(model_id) or {}
        fields["api_key"] = _unmask_value(fields["api_key"], existing.get("api_key", ""))
        if not fields["api_key"]:
            fields.pop("api_key")
    # model_manager.update 会自动失效 _client_cache 并触发 on_change 回调
    # （已在 runtime._attach_refresh_method 里注册了 refresh_legacy_clients），
    # 这里不用再手动调任何 refresh——见 ops_platform/model_manager.py 顶部 docstring。
    record = _runtime().model_manager.update(model_id, **fields)
    return jsonify(_strip_secrets(record, ("api_key",)))


@admin_bp.delete("/models/<model_id>")
@_login_required("admin")
def delete_model(model_id):
    _runtime().model_manager.delete(model_id)
    return ("", 204)


# ---------- skills (read-only registry view) ----------

@admin_bp.get("/skills")
@_login_required()
def list_skills():
    visibility = "user" if g.current_user["role"] != "admin" else None
    items = _runtime().skill_registry.list(visibility=visibility, only_enabled=False)
    return jsonify([s.public_dict() for s in items])


@admin_bp.post("/skills/<code>/invoke")
@_login_required()
def invoke_skill(code):
    """admin/user 都能从平台直接调用一个 skill（用于调试 / 给外部模型用）。"""
    body = request.get_json(silent=True) or {}
    params = body.get("params") or {}
    selected = body.get("selected_connections") or {}
    from ops_platform.context import SkillContext

    ctx = SkillContext(
        runtime=_runtime(),
        user=g.current_user,
        session_id=body.get("session_id"),
        selected_connections=selected,
    )
    envelope = _runtime().skill_invoker.invoke(code, params, ctx)
    return jsonify(envelope)


@admin_bp.post("/agent/ask")
@_login_required()
def agent_ask():
    """让平台 LLM agent 跑一轮 ask 循环。给 chainlit 之外的运维测试 / 自动化用。

    Request body::
        {
          "message": "192.168.2.124 /data 告警了，帮我排查",
          "selected_connections": { "host_agent": "<connection_id>" },   # 可选
          "session_id": "test-1",                                        # 可选
          "max_steps": 8                                                  # 可选
        }

    返回 ``AgentOutcome.__dict__``：``message`` (LLM 最终五段式报告)、
    ``trace`` (完整工具调用链)、``pending_actions`` (写操作待确认列表)。

    没配模型 / 模型超时 / 任意环节挂掉 → 500 + ``{"error": "..."}``，方便排错。
    """
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message_required"}), 400

    from ops_agent import UnifiedOpsAgent
    try:
        agent = UnifiedOpsAgent(
            _runtime(),
            max_steps=int(body.get("max_steps") or 8),
        )
    except Exception as exc:    # noqa: BLE001
        # 通常是模型没配
        return jsonify({"error": "agent_init_failed", "message": str(exc)}), 500

    try:
        outcome = agent.ask(
            message,
            user=g.current_user,
            session_id=body.get("session_id"),
            selected_connections=body.get("selected_connections") or {},
        )
    except Exception as exc:    # noqa: BLE001
        logger.exception("agent.ask 失败")
        return jsonify({"error": "agent_ask_failed", "message": str(exc)}), 500

    return jsonify({
        "message": outcome.message,
        "trace": outcome.trace,
        "pending_actions": outcome.pending_actions,
        # token usage（本次 ask 累计），方便 admin 测试时观测成本
        "usage": getattr(outcome, "usage", {}) or {},
        "trace_summary": [
            {
                "tool": t.get("tool_name"),
                "args": t.get("tool_args"),
                "status": t.get("status"),
                "latency_ms": t.get("latency_ms"),
            }
            for t in outcome.trace
        ],
    })


# ---------- skill call audit ----------

@admin_bp.get("/skill-calls")
@_login_required("admin")
def skill_calls():
    limit = int(request.args.get("limit") or 100)
    return jsonify(_store().list_skill_calls(limit=limit))


@admin_bp.get("/token-usage")
@_login_required("admin")
def token_usage():
    """平台累计 token 消耗（汇总 chat_message.metadata.usage）。"""
    return jsonify(_store().sum_token_usage())


# ---------- runbooks（图执行剧本管理）----------

@admin_bp.get("/runbooks")
@_login_required("admin")
def list_runbooks_api():
    items = _store().list_runbooks()
    # 列表视图返回简版
    return jsonify([
        {
            "key": it["key"],
            "title": it["title"],
            "description": it.get("description", ""),
            "triggers": it.get("triggers", []),
            "inputs": it.get("inputs", []),
            "enabled": it.get("enabled", True),
            "version": it.get("version", 1),
            "node_count": len((it.get("definition") or {}).get("nodes") or {}),
            "updated_at": it.get("updated_at"),
            "updated_by": it.get("updated_by"),
        } for it in items
    ])


@admin_bp.get("/runbooks/<key>")
@_login_required("admin")
def get_runbook_api(key):
    rec = _store().get_runbook(key)
    if not rec:
        return jsonify({"error": "not_found"}), 404
    return jsonify(rec)


@admin_bp.put("/runbooks/<key>")
@_login_required("admin")
def upsert_runbook_api(key):
    from ops_platform.runbook_engine import (
        RunbookLoadError, load_runbook_from_dict, validate_runbook,
    )

    body = request.get_json(silent=True) or {}
    definition = body.get("definition")
    if not isinstance(definition, dict):
        return jsonify({"error": "definition_required",
                        "message": "definition 必须是 JSON 对象"}), 400

    # 强制 key 一致
    definition["key"] = key
    if not definition.get("title"):
        definition["title"] = body.get("title") or key

    # 加载 + 校验
    try:
        rb = load_runbook_from_dict(definition)
    except RunbookLoadError as exc:
        return jsonify({"error": "invalid_definition", "message": str(exc)}), 400

    runtime = _runtime()
    known_skills = {s.code for s in runtime.skill_registry.list(only_enabled=False)}
    write_skills = {s.code for s in runtime.skill_registry.list(only_enabled=False) if not s.read_only}
    errs = validate_runbook(rb, known_skills=known_skills, write_skills=write_skills)
    if errs:
        return jsonify({"error": "validation_failed", "errors": errs}), 400

    rec = _store().upsert_runbook(
        key=key,
        title=rb.title,
        description=rb.description,
        triggers=rb.triggers,
        inputs=rb.inputs,
        definition=definition,
        enabled=rb.enabled,
        updated_by=g.current_user["username"],
    )

    # 改动后立即热加载到内存注册表
    reload_errs = runtime.runbook_registry.reload()
    if reload_errs.get(key):
        return jsonify({"error": "reload_failed", "errors": reload_errs[key], "saved": rec}), 400
    return jsonify(rec)


@admin_bp.delete("/runbooks/<key>")
@_login_required("admin")
def delete_runbook_api(key):
    _store().delete_runbook(key)
    _runtime().runbook_registry.reload()
    return ("", 204)


@admin_bp.post("/runbooks/<key>/validate")
@_login_required("admin")
def validate_runbook_api(key):
    """单独的校验接口：admin 编辑时可不保存先验一次。"""
    from ops_platform.runbook_engine import (
        RunbookLoadError, load_runbook_from_dict, validate_runbook,
    )
    body = request.get_json(silent=True) or {}
    definition = body.get("definition")
    if not isinstance(definition, dict):
        return jsonify({"ok": False, "errors": ["definition 必须是 JSON 对象"]})
    definition["key"] = key
    try:
        rb = load_runbook_from_dict(definition)
    except RunbookLoadError as exc:
        return jsonify({"ok": False, "errors": [str(exc)]})
    runtime = _runtime()
    known_skills = {s.code for s in runtime.skill_registry.list(only_enabled=False)}
    write_skills = {s.code for s in runtime.skill_registry.list(only_enabled=False) if not s.read_only}
    errs = validate_runbook(rb, known_skills=known_skills, write_skills=write_skills)
    return jsonify({"ok": not errs, "errors": errs})


@admin_bp.get("/runbook-runs")
@_login_required("admin")
def list_runbook_runs_api():
    limit = int(request.args.get("limit") or 100)
    rb_key = request.args.get("rb_key") or None
    return jsonify(_store().list_runbook_executions(limit=limit, runbook_key=rb_key))


@admin_bp.get("/runbook-runs/<execution_id>")
@_login_required("admin")
def get_runbook_run_api(execution_id):
    rec = _store().get_runbook_execution(execution_id)
    if not rec:
        return jsonify({"error": "not_found"}), 404
    return jsonify(rec)


# ---------- prompt segments（system prompt 段落库）----------

@admin_bp.get("/prompts")
@_login_required("admin")
def list_prompts():
    from ops_platform.prompts import DEFAULTS, ORDER

    store_records = {r["key"]: r for r in _store().list_prompt_segments()}
    out = []
    for key in ORDER:
        default = DEFAULTS.get(key, {})
        rec = store_records.get(key)
        if rec:
            out.append({
                "key": rec["key"],
                "title": rec.get("title") or default.get("title", key),
                "content": rec.get("content"),
                "default_content": default.get("content", ""),
                "enabled": rec.get("enabled", True),
                "version": rec.get("version", 1),
                "updated_by": rec.get("updated_by"),
                "updated_at": rec.get("updated_at"),
                "is_custom": True,
            })
        else:
            out.append({
                "key": key,
                "title": default.get("title", key),
                "content": default.get("content", ""),
                "default_content": default.get("content", ""),
                "enabled": True,
                "version": 0,
                "updated_by": None,
                "updated_at": None,
                "is_custom": False,
            })
    return jsonify(out)


@admin_bp.put("/prompts/<key>")
@_login_required("admin")
def upsert_prompt(key):
    body = request.get_json(silent=True) or {}
    record = _store().upsert_prompt_segment(
        key=key,
        title=body.get("title", key),
        content=body.get("content", ""),
        enabled=bool(body.get("enabled", True)),
        updated_by=g.current_user["username"],
    )
    return jsonify(record)


@admin_bp.delete("/prompts/<key>")
@_login_required("admin")
def reset_prompt(key):
    """删除 = 回退到出厂默认（DEFAULTS 兜底）"""
    _store().delete_prompt_segment(key)
    return ("", 204)


@admin_bp.get("/prompts/_assembled")
@_login_required("admin")
def show_assembled_prompt():
    from ops_platform.prompts import assemble_from_records
    return jsonify({"content": assemble_from_records(_store().list_prompt_segments())})


# ---------- pending actions（写操作二次确认）----------

@admin_bp.get("/pending-actions")
@_login_required()
def list_pending_actions():
    status = request.args.get("status")
    limit = int(request.args.get("limit") or 50)
    items = _store().list_pending_actions(status=status, limit=limit)
    # 普通用户只看到自己挂起的
    if g.current_user.get("role") != "admin":
        items = [a for a in items if a.get("requested_by") == g.current_user["username"]]
    return jsonify(items)


@admin_bp.get("/pending-actions/<token>")
@_login_required()
def get_pending_action(token):
    record = _store().get_pending_action(token)
    if not record:
        return jsonify({"error": "not_found"}), 404
    return jsonify(record)


@admin_bp.post("/pending-actions/<token>/confirm")
@_login_required()
def confirm_pending_action(token):
    from ops_platform.context import SkillContext

    ctx = SkillContext(
        runtime=_runtime(),
        user=g.current_user,
        session_id=(request.get_json(silent=True) or {}).get("session_id"),
        selected_connections={},
    )
    envelope = _runtime().skill_invoker.confirm(token, ctx)
    return jsonify(envelope)


@admin_bp.post("/pending-actions/<token>/reject")
@_login_required()
def reject_pending_action(token):
    from ops_platform.context import SkillContext

    body = request.get_json(silent=True) or {}
    ctx = SkillContext(
        runtime=_runtime(),
        user=g.current_user,
        session_id=body.get("session_id"),
        selected_connections={},
    )
    envelope = _runtime().skill_invoker.reject(token, ctx, reason=body.get("reason", ""))
    return jsonify(envelope)


# ---------- async tasks（长命令后台任务）-------------------------------------- #

@admin_bp.get("/async-tasks")
@_login_required()
def list_async_tasks():
    """列异步任务。

    支持过滤：status / node / connection_id / session_id / submitted_by。
    普通 user 角色只能看到自己提交的。
    """
    args = request.args
    status = args.get("status") or None
    node = args.get("node") or None
    connection_id = args.get("connection_id") or None
    session_id = args.get("session_id") or None
    submitted_by = args.get("submitted_by") or None
    limit = min(int(args.get("limit") or 200), 1000)

    # 非 admin 默认只看自己的
    if g.current_user.get("role") != "admin":
        submitted_by = g.current_user["username"]

    items = _store().list_async_tasks(
        connection_id=connection_id, node=node, status=status,
        session_id=session_id, submitted_by=submitted_by, limit=limit,
    )
    return jsonify(items)


@admin_bp.get("/async-tasks/<task_id>")
@_login_required()
def get_async_task(task_id):
    """单任务详情。如果还在 running 会触发 service 同步刷新一次。"""
    service = getattr(_runtime(), "async_task_service", None)
    if service is None:
        return jsonify({"error": "async_task_service 未初始化"}), 500
    rec = service.get(task_id, refresh=True)
    if not rec:
        return jsonify({"error": "not_found"}), 404
    # 非 admin 只能看自己的
    if g.current_user.get("role") != "admin" and rec.get("submitted_by") != g.current_user["username"]:
        return jsonify({"error": "forbidden"}), 403
    return jsonify(rec)


@admin_bp.post("/async-tasks/<task_id>/cancel")
@_login_required(role="admin")
def cancel_async_task(task_id):
    """取消任务。admin only——能 cancel 别人的任务是个高敏感操作。"""
    service = getattr(_runtime(), "async_task_service", None)
    if service is None:
        return jsonify({"error": "async_task_service 未初始化"}), 500
    rec = service.cancel(task_id, by=g.current_user.get("username"))
    if not rec:
        return jsonify({"error": "not_found"}), 404
    return jsonify(rec)


@admin_bp.post("/async-tasks/<task_id>/refresh")
@_login_required()
def refresh_async_task(task_id):
    """强制同步一次 agent 状态。前端 UI 上的"刷新"按钮用。"""
    service = getattr(_runtime(), "async_task_service", None)
    if service is None:
        return jsonify({"error": "async_task_service 未初始化"}), 500
    rec = service.get(task_id, refresh=True)
    if not rec:
        return jsonify({"error": "not_found"}), 404
    return jsonify(rec)


# ---------- chat sessions for the current user (for chat history page) ----------

@admin_bp.get("/chat/sessions")
@_login_required()
def chat_sessions():
    # 简化版：当前 store 没有按用户隔离 session，先全部返回
    store = _store()
    if hasattr(store, "chat_sessions") and isinstance(store.chat_sessions, dict):
        return jsonify(list(store.chat_sessions.values()))
    return jsonify([])


# ============================================================================
# Maintenance: 清理 host_agent 残留 sibling 容器
# ============================================================================
#
# 设计动机
# --------
# ``AGENT_MODE=docker_proxy`` 下 agent 用 ``docker run --rm`` 起一次性 sibling
# 容器跑 nsenter。正常情况 sibling 退出 docker 自动 ``--rm`` 销毁。但极端情况
# (docker daemon 卡死 / agent 被 OOM kill / docker run 自身没正常退出)可能
# 留下 stopped 容器占节点磁盘空间。
#
# 本接口给运维一个"应急按钮"——扫所有 host_agent 集群里所有节点上的残留 stopped
# sibling,在 UI 上勾选清掉。直接走 ``client.exec_on_node`` 跑 ``docker ps``/
# ``docker rm``,不需要改 agent 镜像(agent allowed.yml 已经允许 sh/bash,
# 通过 ``sh -c "docker ..."`` 在 host ns 跑 docker 命令)。

import shlex as _shlex


def _ps_stopped_containers_cmd() -> list[str]:
    """构造列 host 上 stopped 容器的命令(sh -c)。

    输出 tab 分隔 5 列:ID / Image / Names / Status / CreatedAt
    """
    fmt = r"{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Status}}\t{{.CreatedAt}}"
    return ["sh", "-c",
            f"docker ps -a --filter status=exited --format '{fmt}'"]


def _parse_ps_output(stdout: str, image_filter: str | None) -> list[dict]:
    """解析 docker ps tab 分隔输出;按 image_filter 子串过滤。

    image_filter 为空 / None 时返回全量。
    """
    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        cid, image, name, status, *rest = parts
        if image_filter and image_filter.lower() not in image.lower():
            continue
        created_at = rest[0] if rest else ""
        rows.append({
            "container_id": cid,
            "image": image,
            "name": name,
            "status": status,
            "created_at": created_at,
        })
    return rows


@admin_bp.get("/maintenance/sibling-containers")
@_login_required(role="admin")
def maintenance_scan_sibling_containers():
    """扫所有 enabled host_agent 集群里每个节点的 stopped 残留容器。

    Query params:
        image_filter: 镜像名子串过滤,默认 ``ai-ops-agent``。传空表示不过滤(看全部 stopped)。

    Returns:
        ``{"image_filter", "connections": [...], "errors": [...]}``
    """
    raw_filter = request.args.get("image_filter")
    image_filter = ("ai-ops-agent" if raw_filter is None else raw_filter).strip()
    runtime = _runtime()
    conn_mgr = runtime.connection_manager

    out_per_conn: list[dict] = []
    errors: list[dict] = []

    for conn in conn_mgr.list(type_code="host_agent"):
        if not conn.get("enabled", True):
            continue
        try:
            client = conn_mgr.get_client(conn["id"])
            nodes = client.list_nodes()
        except Exception as exc:    # noqa: BLE001
            errors.append({
                "connection_id": conn["id"],
                "alias": conn.get("alias") or conn["name"],
                "phase": "list_nodes",
                "error": str(exc),
            })
            continue

        conn_block: dict = {
            "connection_id": conn["id"],
            "alias": conn.get("alias") or conn["name"],
            "kind": (conn.get("config") or {}).get("kind", ""),
            "nodes": [],
        }
        for node in nodes:
            try:
                result = client.exec_on_node(node, _ps_stopped_containers_cmd(), timeout=20)
                if result.returncode != 0:
                    conn_block["nodes"].append({
                        "node": node,
                        "error": result.stderr or result.stdout or "exec failed",
                    })
                    continue
                conn_block["nodes"].append({
                    "node": node,
                    "leftovers": _parse_ps_output(result.stdout, image_filter or None),
                })
            except Exception as exc:    # noqa: BLE001
                conn_block["nodes"].append({"node": node, "error": str(exc)})
        out_per_conn.append(conn_block)

    return jsonify({
        "image_filter": image_filter,
        "connections": out_per_conn,
        "errors": errors,
    })


@admin_bp.post("/maintenance/sibling-containers/cleanup")
@_login_required(role="admin")
def maintenance_cleanup_sibling_containers():
    """按 ``(connection_id, node, container_ids)`` 批量 ``docker rm`` 残留容器。

    Body:
        ``{"items": [{"connection_id": "...", "node": "...", "container_ids": [...]}, ...]}``

    Returns:
        ``{"results": [{"connection_id", "node", "requested", "ok", "stdout"/"error", ...}]}``
    """
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "items must be a non-empty array"}), 400

    runtime = _runtime()
    conn_mgr = runtime.connection_manager
    results: list[dict] = []

    for item in items:
        if not isinstance(item, dict):
            results.append({"error": "item must be dict", "item": item})
            continue
        conn_id = item.get("connection_id")
        node = item.get("node")
        container_ids = item.get("container_ids") or []
        if not conn_id or not node or not container_ids:
            results.append({
                "error": "missing connection_id / node / container_ids",
                "item": item,
            })
            continue
        if not isinstance(container_ids, list) or not all(
            isinstance(c, str) and c.strip() for c in container_ids
        ):
            results.append({
                "error": "container_ids must be non-empty list of strings",
                "item": item,
            })
            continue

        try:
            client = conn_mgr.get_client(conn_id)
        except Exception as exc:    # noqa: BLE001
            results.append({
                "connection_id": conn_id, "node": node,
                "requested": container_ids, "ok": False,
                "error": f"get_client failed: {exc}",
            })
            continue

        ids_str = " ".join(_shlex.quote(str(cid)) for cid in container_ids)
        cmd = ["sh", "-c", f"docker rm {ids_str}"]
        try:
            result = client.exec_on_node(node, cmd, timeout=30)
            results.append({
                "connection_id": conn_id, "node": node,
                "requested": container_ids,
                "ok": result.returncode == 0,
                "stdout": result.stdout, "stderr": result.stderr,
                "returncode": result.returncode,
            })
        except Exception as exc:    # noqa: BLE001
            results.append({
                "connection_id": conn_id, "node": node,
                "requested": container_ids, "ok": False,
                "error": str(exc),
            })

    # 写操作必须落审计 —— admin maintenance 跟 skill 写操作一视同仁
    try:
        runtime.store.save_skill_call(
            skill_code="admin.maintenance.cleanup_sibling_containers",
            connection_id=None,
            session_id=None,
            user=(getattr(g, "current_user", None) or {}).get("username"),
            args={"items": items},
            result={"results": results},
            status="ok",
            error=None,
            latency_ms=0,
        )
    except Exception:   # pragma: no cover
        logger.exception("写 admin maintenance 审计失败(不阻塞)")

    return jsonify({"results": results})


# ---------- bootstrap ----------

def init_admin(app, runtime, config_cls) -> None:
    """主应用调用：注册蓝图、写入默认 admin、写入 JWT secret。"""
    app.config.setdefault("ADMIN_JWT_SECRET",
                          config_cls.__dict__.get("ADMIN_JWT_SECRET") or "change-me-in-prod")
    app.register_blueprint(admin_bp)

    # 创建默认 admin
    if not runtime.store.list_users():
        username = getattr(config_cls, "ADMIN_BOOTSTRAP_USERNAME", "admin")
        password = getattr(config_cls, "ADMIN_BOOTSTRAP_PASSWORD", "admin123")
        runtime.store.create_user(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            display_name="平台管理员",
        )
        logger.info("已创建默认 admin 用户：%s（请尽快改密）", username)
