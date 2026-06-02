from __future__ import annotations

import json
import logging
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from ops_platform.context import SkillContext
from ops_platform.registry import SkillRegistry, SkillSpec


logger = logging.getLogger(__name__)


# ---------- 审计日志脱敏 ---------- #
#
# 设计原因
# --------
# admin 在 host_run_command 里跑 ``mysql -uroot -p<password>`` /
# ``curl -H 'Authorization: Bearer xxx'`` 时,凭证会原样进入 skill 的 params,
# 进而被 _execute() 落到 ``platform_skill_call.args_json`` 明文存储。
# 后续日志导出 / DB 备份 / admin 后台查历史都会暴露——典型的"审计日志反成
# 数据泄露源"反模式。
#
# 这里做两层脱敏:
# 1. **键名匹配**:params 顶层及一层嵌套的 key 名命中敏感词 → 值替换成 ``***``
# 2. **command 字符串内的模式**:host_run_command / host_run_command_async 等
#    skill 的 ``command`` 参数是 shell 字符串,要单独 regex 替换密码/token 段

_SENSITIVE_KEY_PATTERNS = (
    "password", "passwd", "api_key", "apikey", "secret", "token",
    "authorization", "auth_token", "private_key", "credential",
)

# command 字符串里的常见凭证模式
_COMMAND_REDACT_PATTERNS = [
    # mysql -uroot -p<password> / mysql -p<password>
    (re.compile(r"(-p)(\S+)"), r"\1***"),
    # curl -H 'Authorization: Bearer xxx' / -H "X-Api-Key: xxx"
    # 注意：替换 header 名后**所有**剩余值（包括 "Bearer xxx"、可能含空格的多 token）
    # 直到结束引号或行尾——而不是只截到第一个空格。
    (re.compile(r"(-H\s+['\"]?(?:Authorization|X-Api-Key|X-Auth-Token)\s*:\s*)[^'\"\n]+",
                re.IGNORECASE), r"\1***"),
    # URL 里的 user:password@host
    (re.compile(r"(://[^:/\s]+:)([^@/\s]+)(@)"), r"\1***\3"),
    # KEY=VALUE 形式的环境变量（在命令里）。注意 KEY 可能有前缀（DATABASE_PASSWORD），
    # 所以前面允许任意 \w 前缀，而不是用 \b 做严格边界。
    (re.compile(r"(\w*(?:PASSWORD|PASSWD|TOKEN|API_KEY|APIKEY|SECRET)\w*\s*=)(\S+)",
                re.IGNORECASE), r"\1***"),
]


def _is_sensitive_key(key: str) -> bool:
    """命中 _SENSITIVE_KEY_PATTERNS 任一子串即视为敏感。

    把 ``-`` 也归一成 ``_``，让 ``X-API-KEY`` / ``X-Api-Key`` 这种 HTTP header 命名
    也能被识别（不然 ``api-key`` 不包含 ``api_key`` 子串就会漏过）。
    """
    k = key.lower().replace("-", "_")
    return any(s in k for s in _SENSITIVE_KEY_PATTERNS)


def _redact_command_string(cmd: str) -> str:
    """对 shell command 字符串做凭证模式替换。已 redact 字段返回新字符串。"""
    if not isinstance(cmd, str):
        return cmd
    out = cmd
    for pattern, repl in _COMMAND_REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _redact_audit_args(args: dict, _depth: int = 0) -> dict:
    """对将要落审计表的 params 做脱敏,**不修改原 dict**。

    递归处理 dict / list,深度上限 4 层(skill 参数嵌套通常 1-2 层,留余量)。

    脱敏规则:
    1. key 命中 ``_SENSITIVE_KEY_PATTERNS`` → 值替换 ``***``
    2. key 是 ``command`` 且值是字符串 → 跑 ``_redact_command_string``
       (host_run_command / host_run_command_async 等的核心字段)
    3. 其他 dict/list 递归;原始类型原样
    """
    if _depth > 4 or not isinstance(args, dict):
        return args
    out: dict = {}
    for k, v in args.items():
        if _is_sensitive_key(k):
            out[k] = "***"
        elif k == "command" and isinstance(v, str):
            out[k] = _redact_command_string(v)
        elif isinstance(v, dict):
            out[k] = _redact_audit_args(v, _depth + 1)
        elif isinstance(v, list):
            out[k] = [
                _redact_audit_args(item, _depth + 1) if isinstance(item, dict)
                else (_redact_command_string(item) if isinstance(item, str) and "command" in k.lower()
                      else item)
                for item in v
            ]
        else:
            out[k] = v
    return out


def _contains_stub_data(obj: Any, depth: int = 0) -> bool:
    """递归找 ``_stub_data: True`` 标记；最多看 5 层避免无限递归。"""
    if depth > 5:
        return False
    if isinstance(obj, dict):
        if obj.get("_stub_data") is True:
            return True
        for v in obj.values():
            if _contains_stub_data(v, depth + 1):
                return True
    elif isinstance(obj, list):
        for v in obj[:30]:  # 仅看前 30 个元素，防止大列表性能崩
            if _contains_stub_data(v, depth + 1):
                return True
    return False


class SkillInvoker:
    """统一执行入口；被 chainlit / Flask API / MCP server 共用。

    路由策略：
        - 只读 skill (`read_only=True`)：直接执行。
        - 写操作 skill：第一次调用进入"待确认"状态，落到 ``platform_pending_action`` 表，
          返回 ``status="needs_confirmation"`` 的 envelope（带 token + 预览）。
          调用方（chainlit / admin UI / MCP client）拿到 token，让用户点"确认"，
          再调 ``confirm(token, ctx)`` 真正执行。
        - 写操作 skill 且 ``requires_admin_approval=True``：必须由 admin 角色用户调
          ``confirm()``，否则拒绝。
    """

    def __init__(self, registry: SkillRegistry, store: Any) -> None:
        self.registry = registry
        self.store = store

    # ---------- 主入口 ----------

    def invoke(
        self,
        code: str,
        params: dict[str, Any],
        ctx: SkillContext,
    ) -> dict[str, Any]:
        try:
            spec = self.registry.get(code)
        except KeyError:
            return self._error_envelope(
                f"skill {code!r} 未注册",
                code="unknown_skill",
            )

        # RBAC 收口：visibility=admin 的 skill 只允许 role=admin 调用。
        # 之前依赖 SkillRegistry.list(visibility="user") 在 tool schema 层过滤——
        # LLM 路径下 user 看不到 admin-only 工具,但 HTTP API / runbook 引用 / 内部
        # 调用都能直接绕过这层"看不见"。本层是真正的执行门禁。
        user_role = (ctx.user or {}).get("role") or "user"
        if spec.visibility == "admin" and user_role != "admin":
            return self._error_envelope(
                f"skill {code!r} 仅限 admin 角色调用（当前角色:{user_role}）",
                code="forbidden",
            )

        if spec.read_only:
            return self._execute(spec, params, ctx)
        # 写操作：进入两步流程，先生成 pending action
        return self._prepare(spec, params, ctx)

    # ---------- prepare ----------

    def _prepare(self, spec: SkillSpec, params: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        token = secrets.token_urlsafe(18)
        expires_at = (datetime.now(UTC) + timedelta(seconds=spec.confirmation_ttl_seconds)).isoformat()
        connection_id = params.get("connection_id")
        if not connection_id and spec.required_connection_type:
            default = ctx.runtime.connection_manager.get_default(spec.required_connection_type)
            if default:
                connection_id = default["id"]

        preview = {
            "skill_code": spec.code,
            "skill_name": spec.name,
            "category": spec.category,
            "args": params,
            "connection_id": connection_id,
            "requires_admin_approval": spec.requires_admin_approval,
        }

        record = self.store.create_pending_action(
            token=token,
            skill_code=spec.code,
            args=params,
            connection_id=connection_id,
            requires_admin_approval=spec.requires_admin_approval,
            requested_by=(ctx.user or {}).get("username"),
            session_id=ctx.session_id,
            expires_at=expires_at,
            preview=preview,
        )

        return {
            "skill": spec.code,
            "status": "needs_confirmation",
            "read_only": False,
            "pending_token": token,
            "expires_at": expires_at,
            "preview": preview,
            "message": f"写操作 '{spec.name}' 已挂起，等待用户确认。token={token}",
            "result": {
                "_pending": True,
                "token": token,
                "skill": spec.code,
                "args": params,
                "requires_admin_approval": spec.requires_admin_approval,
            },
        }

    # ---------- confirm ----------

    def confirm(self, token: str, ctx: SkillContext) -> dict[str, Any]:
        record = self.store.get_pending_action(token)
        if not record:
            return self._error_envelope("未知的待确认 token", code="invalid_token")
        if record["status"] != "pending":
            return self._error_envelope(f"该操作已 {record['status']}，无法重复确认", code="bad_state")
        if self._is_expired(record):
            self.store.update_pending_action_status(token, status="expired")
            return self._error_envelope("待确认操作已超时", code="expired")

        # 防止 token 跨会话窃用：聊天用户的 token 泄露后,攻击者在自己会话里重放。
        # **例外：admin 可跨会话确认**——admin 在后台 PendingActions 页确认是合法的
        # 跨会话审批操作（后台 session ≠ 原聊天 session），这是 admin 的职责,不能挡。
        # 所以只对**非 admin**强制 session 绑定。
        recorded_session = record.get("session_id")
        is_admin = (ctx.user or {}).get("role") == "admin"
        if (not is_admin) and recorded_session and recorded_session != ctx.session_id:
            return self._error_envelope(
                "token 与当前会话不匹配，可能被跨会话重放",
                code="session_mismatch",
            )

        if record.get("requires_admin_approval") and not is_admin:
            return self._error_envelope("该操作需管理员确认", code="forbidden")

        spec = self.registry.get(record["skill_code"])
        envelope = self._execute(
            spec,
            record["args_json"] if isinstance(record["args_json"], dict) else json.loads(record["args_json"] or "{}"),
            ctx,
            extra_audit={"confirmation_token": token},
        )
        self.store.update_pending_action_status(
            token,
            status="executed" if envelope["status"] == "ok" else "failed",
            decided_by=(ctx.user or {}).get("username"),
        )
        envelope["pending_token"] = token
        envelope["pending_status"] = "executed" if envelope["status"] == "ok" else "failed"
        return envelope

    # ---------- reject ----------

    def reject(self, token: str, ctx: SkillContext, reason: str = "") -> dict[str, Any]:
        record = self.store.get_pending_action(token)
        if not record:
            return self._error_envelope("未知的待确认 token", code="invalid_token")
        if record["status"] != "pending":
            return self._error_envelope(f"该操作已 {record['status']}", code="bad_state")
        # 同 confirm()：非 admin 强制 session 绑定；admin 可跨会话（后台审批职责）。
        recorded_session = record.get("session_id")
        is_admin = (ctx.user or {}).get("role") == "admin"
        if (not is_admin) and recorded_session and recorded_session != ctx.session_id:
            return self._error_envelope(
                "token 与当前会话不匹配，可能被跨会话重放",
                code="session_mismatch",
            )
        updated = self.store.update_pending_action_status(
            token,
            status="rejected",
            decided_by=(ctx.user or {}).get("username"),
            reject_reason=reason or "user_rejected",
        )
        return {
            "skill": record["skill_code"],
            "status": "rejected",
            "pending_token": token,
            "result": {"_rejected": True, "token": token, "reason": reason or "user_rejected"},
            "message": "用户已拒绝该操作。",
            "record": updated,
        }

    # ---------- 内部 ----------

    def _execute(
        self,
        spec: SkillSpec,
        params: dict[str, Any],
        ctx: SkillContext,
        *,
        extra_audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.time()
        status = "ok"
        result: dict[str, Any]
        error: str | None = None
        try:
            result = self.registry.execute(spec.code, params, ctx)
        except Exception as exc:
            status = "error"
            error = str(exc)
            result = {"error": error, "skill": spec.code, "params": params}
            logger.exception("skill %s 执行失败", spec.code)
        latency_ms = int((time.time() - started) * 1000)

        # 检测 stub 数据：如果 result 里出现 _stub_data: True，自动 emit 警告信号
        # 让 agent 在最终报告里告诉用户"这是模拟数据"
        if isinstance(result, dict) and _contains_stub_data(result):
            from ops_platform.signals import attach as _attach, signal as _sig
            _attach(result, [_sig(
                "stub_data_returned",
                severity="warning",
                evidence=(
                    f"skill {spec.code} 返回了 _stub_data=True 的模拟数据。"
                    "这意味着 connection 处于 stub 模式，并非真实监控数据。"
                    "请到管理后台关掉对应 connection 的 use_stub 开关并填真实凭证。"
                ),
            )])

        try:
            # 审计前脱敏：详见模块顶部 _redact_audit_args 的设计说明。
            # 原 params 不变（spec.handler 已经基于原 params 跑过），脱敏只影响审计落库。
            audit_args = _redact_audit_args(params)
            if extra_audit:
                # ``_extra`` 是平台生成的审计元数据（如 confirmation_token），**不是用户输入**，
                # 不能脱敏——审计页靠 ``_extra.confirmation_token`` 反查写操作发起人。
                # （之前误用 _redact_audit_args 把 confirmation_token 当 "token" 脱成 ***，
                #   破坏了溯源功能。）
                audit_args = {**audit_args, "_extra": extra_audit}
            self.store.save_skill_call(
                skill_code=spec.code,
                connection_id=params.get("connection_id"),
                session_id=ctx.session_id,
                user=(ctx.user or {}).get("username"),
                args=audit_args,
                result=result if status == "ok" else None,
                status=status,
                error=error,
                latency_ms=latency_ms,
            )
        except Exception:  # pragma: no cover
            logger.exception("写 skill_call 审计失败")

        return {
            "skill": spec.code,
            "status": status,
            "latency_ms": latency_ms,
            "result": result,
            "read_only": spec.read_only,
        }

    @staticmethod
    def _is_expired(record: dict) -> bool:
        try:
            return datetime.fromisoformat(record["expires_at"]) < datetime.now(UTC)
        except Exception:
            return False

    @staticmethod
    def _error_envelope(message: str, *, code: str = "error") -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": code,
            "message": message,
            "result": {"error": message, "error_code": code},
        }

    # ---------- 给模型看的压缩序列化（"digest"）----------

    # 大文本字段 → 头尾保留各 60% / 40%，中间用占位符切掉
    _LARGE_TEXT_FIELDS = {
        "logs", "matched_logs", "events", "describe",
        "stdout", "stderr", "summary",
    }
    # **配置类查询的特殊字段**——这些字段一旦命中就给更宽的预算，因为用户问
    # "看 stack 配置" 时希望模型把完整 YAML/JSON 贴出来。
    # ``stdout`` / ``parsed`` 在 kube_query / swarm_query / host_query 里都是
    # 主要内容字段，太小就丢配置原文。
    _CONFIG_FIELDS = {"stdout", "parsed"}
    _MAX_FIELD_CHARS = 2000          # 普通文本字段上限
    _MAX_CONFIG_CHARS = 6000         # 配置/raw 字段上限（约 200 行 YAML）
    _MAX_LIST_LEN = 12               # 列表只保留前 N 项
    _TOTAL_BUDGET = 24_000           # 整个 tool message 上限（≈ 6K token，给配置类留足空间）

    @classmethod
    def serialize_for_model(cls, envelope: dict[str, Any]) -> str:
        """压缩 envelope 给模型读，原始完整数据仍保留在 trace 里。

        压缩策略：
          - 大文本字段截首尾 + 中间省略占位
          - 列表只保留前 N 项
          - 私有字段（``_xxx``）一律删除，但 ``_signals`` 保留（小但高价值）
          - 最终再卡 8KB 总长，超出再硬截
        """
        # 没有 result 字段（如 error envelope）：直接序列化整个 envelope，截断
        if not isinstance(envelope.get("result"), dict):
            return cls._final_cap(json.dumps(envelope, ensure_ascii=False, default=str))

        compact = cls._compact_value(envelope["result"])
        out: dict[str, Any] = {
            "status": envelope.get("status"),
            "latency_ms": envelope.get("latency_ms"),
            "result": compact,
        }
        for k in ("pending_token", "expires_at", "preview", "error_code", "message"):
            if envelope.get(k) is not None:
                out[k] = envelope[k]
        return cls._final_cap(json.dumps(out, ensure_ascii=False, default=str))

    @classmethod
    def _compact_value(cls, v: Any, depth: int = 0, *, field_key: str | None = None) -> Any:
        """递归压缩 result 给模型看。``field_key`` 用于识别"配置类大字段"放宽截断。"""
        if depth > 8:  # 防御深嵌套
            return "[...]"
        if isinstance(v, str):
            # 配置类大字段（stdout / parsed 等）—— 用更宽的预算保证用户能看到完整 YAML/JSON
            limit = cls._MAX_CONFIG_CHARS if field_key in cls._CONFIG_FIELDS else cls._MAX_FIELD_CHARS
            if len(v) <= limit:
                return v
            head_len = int(limit * 0.7)   # 配置类偏向保留头部（image / env / labels）
            tail_len = limit - head_len
            return (
                v[:head_len]
                + f"\n...[middle {len(v) - limit}c truncated; full in trace]\n"
                + v[-tail_len:]
            )
        if isinstance(v, list):
            if len(v) > cls._MAX_LIST_LEN:
                kept = [cls._compact_value(x, depth + 1) for x in v[: cls._MAX_LIST_LEN]]
                kept.append(f"[...+{len(v) - cls._MAX_LIST_LEN} more items truncated; full in trace]")
                return kept
            return [cls._compact_value(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {
                # 把当前 key 传下去，让子字符串能识别"自己处在配置类字段路径上"
                k: cls._compact_value(val, depth + 1, field_key=k if isinstance(k, str) else None)
                for k, val in v.items()
                if not (isinstance(k, str) and k.startswith("_") and k != "_signals")
            }
        return v

    @classmethod
    def _final_cap(cls, s: str) -> str:
        if len(s) <= cls._TOTAL_BUDGET:
            return s
        return s[: cls._TOTAL_BUDGET] + f"...[+{len(s) - cls._TOTAL_BUDGET}c hard-capped; trace has full data]"
