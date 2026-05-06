from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from ops_platform.context import SkillContext
from ops_platform.registry import SkillRegistry, SkillSpec


logger = logging.getLogger(__name__)


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
        spec = self.registry.get(code)
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

        if record.get("requires_admin_approval") and (ctx.user or {}).get("role") != "admin":
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

        try:
            audit_args = dict(params)
            if extra_audit:
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
    _MAX_FIELD_CHARS = 2000          # 单个文本字段最大字符数
    _MAX_LIST_LEN = 12               # 列表只保留前 N 项
    _TOTAL_BUDGET = 8000             # 整个 tool message 最终字符上限

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
    def _compact_value(cls, v: Any, depth: int = 0) -> Any:
        if depth > 8:  # 防御深嵌套
            return "[...]"
        if isinstance(v, str):
            if len(v) <= cls._MAX_FIELD_CHARS:
                return v
            head_len = int(cls._MAX_FIELD_CHARS * 0.6)
            tail_len = cls._MAX_FIELD_CHARS - head_len
            return (
                v[:head_len]
                + f"\n...[middle {len(v) - cls._MAX_FIELD_CHARS}c truncated; full in trace]\n"
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
                k: cls._compact_value(val, depth + 1)
                for k, val in v.items()
                if not (isinstance(k, str) and k.startswith("_") and k != "_signals")
            }
        return v

    @classmethod
    def _final_cap(cls, s: str) -> str:
        if len(s) <= cls._TOTAL_BUDGET:
            return s
        return s[: cls._TOTAL_BUDGET] + f"...[+{len(s) - cls._TOTAL_BUDGET}c hard-capped; trace has full data]"
