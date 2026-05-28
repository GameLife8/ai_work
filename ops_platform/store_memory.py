"""InMemoryPlatformStore —— 内存版本的 platform store（开发/单测用）。

完整设计说明见 ops_platform/store/__init__.py —— 本文件只承担类定义,
导入和工具函数由父模块负责。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


class InMemoryPlatformStore:
    """配合 InMemoryStore 使用，承担 user/connection/model_config/skill_call/pending_action 的存储。"""

    def __init__(self) -> None:
        self.users: list[dict] = []
        self.connections: list[dict] = []
        self.model_configs: list[dict] = []
        self.skill_calls: list[dict] = []
        self.pending_actions: dict[str, dict] = {}
        self._lock = threading.RLock()

    # users
    def list_users(self) -> list[dict]:
        return [deepcopy(u) for u in self.users]

    def get_user(self, user_id: str) -> dict | None:
        return next((deepcopy(u) for u in self.users if u["id"] == user_id), None)

    def get_user_by_username(self, username: str) -> dict | None:
        return next((deepcopy(u) for u in self.users if u["username"] == username), None)

    def create_user(self, *, username: str, password_hash: str, role: str,
                    display_name: str = "") -> dict:
        with self._lock:
            if any(u["username"] == username for u in self.users):
                raise ValueError(f"用户名已存在：{username}")
            now = _now()
            record = {
                "id": _new_id(),
                "username": username,
                "password_hash": password_hash,
                "role": role,
                "display_name": display_name or username,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            }
            self.users.append(record)
            return deepcopy(record)

    def update_user(self, user_id: str, **fields) -> dict:
        with self._lock:
            for u in self.users:
                if u["id"] == user_id:
                    for k, v in fields.items():
                        if k in {"id", "username", "created_at"}:
                            continue
                        u[k] = v
                    u["updated_at"] = _now()
                    return deepcopy(u)
        raise KeyError(user_id)

    def delete_user(self, user_id: str) -> None:
        with self._lock:
            self.users = [u for u in self.users if u["id"] != user_id]

    # connections
    def list_connections(self, type_code: str | None = None) -> list[dict]:
        items = self.connections
        if type_code:
            items = [c for c in items if c["type_code"] == type_code]
        # 兜底：老记录可能没 tags 字段
        return [{"tags": [], **deepcopy(c)} if "tags" not in c else deepcopy(c) for c in items]

    def get_connection(self, connection_id: str) -> dict | None:
        for c in self.connections:
            if c["id"] == connection_id:
                out = deepcopy(c)
                out.setdefault("tags", [])
                return out
        return None

    def create_connection(self, *, type_code: str, name: str, alias: str,
                          config: dict, is_default: bool, created_by: str | None,
                          tags: list[str] | None = None) -> dict:
        with self._lock:
            now = _now()
            if is_default:
                for c in self.connections:
                    if c["type_code"] == type_code:
                        c["is_default"] = False
            record = {
                "id": _new_id(),
                "type_code": type_code,
                "name": name,
                "alias": alias or "",
                "config": deepcopy(config),
                "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
                "is_default": bool(is_default),
                "enabled": True,
                "status": "unknown",
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            self.connections.append(record)
            return deepcopy(record)

    def update_connection(self, connection_id: str, **fields) -> dict:
        with self._lock:
            for c in self.connections:
                if c["id"] == connection_id:
                    if fields.get("is_default"):
                        for other in self.connections:
                            if other["type_code"] == c["type_code"] and other["id"] != connection_id:
                                other["is_default"] = False
                    for k, v in fields.items():
                        if k in {"id", "created_at"}:
                            continue
                        c[k] = v
                    c["updated_at"] = _now()
                    return deepcopy(c)
        raise KeyError(connection_id)

    def delete_connection(self, connection_id: str) -> None:
        with self._lock:
            self.connections = [c for c in self.connections if c["id"] != connection_id]

    # model configs
    def list_model_configs(self) -> list[dict]:
        return [deepcopy(m) for m in self.model_configs]

    def get_model_config(self, model_id: str) -> dict | None:
        return next((deepcopy(m) for m in self.model_configs if m["id"] == model_id), None)

    def create_model_config(self, *, provider: str, name: str, base_url: str,
                            api_key: str, model: str, is_default: bool,
                            created_by: str | None) -> dict:
        with self._lock:
            if is_default:
                for m in self.model_configs:
                    m["is_default"] = False
            now = _now()
            record = {
                "id": _new_id(),
                "provider": provider,
                "name": name,
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "timeout_seconds": 120,
                "is_default": bool(is_default),
                "enabled": True,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            self.model_configs.append(record)
            return deepcopy(record)

    def update_model_config(self, model_id: str, **fields) -> dict:
        with self._lock:
            for m in self.model_configs:
                if m["id"] == model_id:
                    if fields.get("is_default"):
                        for other in self.model_configs:
                            if other["id"] != model_id:
                                other["is_default"] = False
                    for k, v in fields.items():
                        if k in {"id", "created_at"}:
                            continue
                        m[k] = v
                    m["updated_at"] = _now()
                    return deepcopy(m)
        raise KeyError(model_id)

    def delete_model_config(self, model_id: str) -> None:
        with self._lock:
            self.model_configs = [m for m in self.model_configs if m["id"] != model_id]

    # skill_call audit
    def save_skill_call(self, **fields) -> int:
        with self._lock:
            call_id = len(self.skill_calls) + 1
            record = {
                "id": call_id,
                "skill_code": fields.get("skill_code"),
                "connection_id": fields.get("connection_id"),
                "session_id": fields.get("session_id"),
                "user": fields.get("user"),
                "args_json": deepcopy(fields.get("args") or {}),
                "result_json": deepcopy(fields.get("result") or {}),
                "status": fields.get("status", "ok"),
                "error": fields.get("error"),
                "latency_ms": fields.get("latency_ms"),
                "created_at": _now(),
            }
            self.skill_calls.append(record)
            return call_id

    def list_skill_calls(self, *, limit: int = 100) -> list[dict]:
        return [deepcopy(c) for c in self.skill_calls[-limit:]]

    # pending actions
    def create_pending_action(self, *, token: str, skill_code: str, args: dict,
                              connection_id: str | None, requires_admin_approval: bool,
                              requested_by: str | None, session_id: str | None,
                              expires_at: str, preview: dict) -> dict:
        with self._lock:
            record = {
                "token": token,
                "skill_code": skill_code,
                "args_json": deepcopy(args),
                "connection_id": connection_id,
                "requires_admin_approval": bool(requires_admin_approval),
                "requested_by": requested_by,
                "session_id": session_id,
                "status": "pending",
                "preview_json": deepcopy(preview),
                "created_at": _now(),
                "expires_at": expires_at,
                "decided_at": None,
                "decided_by": None,
                "reject_reason": None,
                "skill_call_id": None,
            }
            self.pending_actions[token] = record
            return deepcopy(record)

    def get_pending_action(self, token: str) -> dict | None:
        rec = self.pending_actions.get(token)
        return deepcopy(rec) if rec else None

    def list_pending_actions(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        items = list(self.pending_actions.values())
        if status:
            items = [a for a in items if a["status"] == status]
        items.sort(key=lambda a: a["created_at"], reverse=True)
        return [deepcopy(a) for a in items[:limit]]

    def update_pending_action_status(self, token: str, *, status: str,
                                     decided_by: str | None = None,
                                     reject_reason: str | None = None,
                                     skill_call_id: int | None = None) -> dict | None:
        with self._lock:
            rec = self.pending_actions.get(token)
            if not rec:
                return None
            rec["status"] = status
            rec["decided_at"] = _now()
            if decided_by is not None:
                rec["decided_by"] = decided_by
            if reject_reason is not None:
                rec["reject_reason"] = reject_reason
            if skill_call_id is not None:
                rec["skill_call_id"] = skill_call_id
            return deepcopy(rec)

    # prompt segments
    def __init_prompt_segments_attr__(self) -> None:
        if not hasattr(self, "prompt_segments"):
            self.prompt_segments = {}

    def list_prompt_segments(self) -> list[dict]:
        self.__init_prompt_segments_attr__()
        return [deepcopy(v) for v in self.prompt_segments.values()]

    def get_prompt_segment(self, key: str) -> dict | None:
        self.__init_prompt_segments_attr__()
        rec = self.prompt_segments.get(key)
        return deepcopy(rec) if rec else None

    def upsert_prompt_segment(self, *, key: str, title: str, content: str,
                              enabled: bool = True, updated_by: str | None = None) -> dict:
        self.__init_prompt_segments_attr__()
        with self._lock:
            existing = self.prompt_segments.get(key) or {}
            rec = {
                "key": key,
                "title": title or existing.get("title", key),
                "content": content,
                "enabled": bool(enabled),
                "version": int(existing.get("version", 0)) + 1,
                "updated_by": updated_by,
                "updated_at": _now(),
            }
            self.prompt_segments[key] = rec
            return deepcopy(rec)

    def delete_prompt_segment(self, key: str) -> None:
        self.__init_prompt_segments_attr__()
        with self._lock:
            self.prompt_segments.pop(key, None)

    # ---- runbooks ----
    def __init_runbooks_attr__(self) -> None:
        if not hasattr(self, "runbooks"):
            self.runbooks: dict[str, dict] = {}
        if not hasattr(self, "runbook_executions"):
            self.runbook_executions: dict[str, dict] = {}

    def list_runbooks(self) -> list[dict]:
        self.__init_runbooks_attr__()
        return [deepcopy(v) for v in self.runbooks.values()]

    def get_runbook(self, key: str) -> dict | None:
        self.__init_runbooks_attr__()
        rec = self.runbooks.get(key)
        return deepcopy(rec) if rec else None

    def upsert_runbook(self, *, key: str, title: str, description: str,
                       triggers: list, inputs: list, definition: dict,
                       enabled: bool = True, updated_by: str | None = None) -> dict:
        self.__init_runbooks_attr__()
        with self._lock:
            existing = self.runbooks.get(key) or {}
            rec = {
                "key": key,
                "title": title,
                "description": description or "",
                "triggers": list(triggers or []),
                "inputs": list(inputs or []),
                "definition": deepcopy(definition or {}),
                "enabled": bool(enabled),
                "version": int(existing.get("version", 0)) + 1,
                "updated_by": updated_by,
                "updated_at": _now(),
            }
            self.runbooks[key] = rec
            return deepcopy(rec)

    def delete_runbook(self, key: str) -> None:
        self.__init_runbooks_attr__()
        with self._lock:
            self.runbooks.pop(key, None)

    def save_runbook_execution(self, **fields) -> str:
        self.__init_runbooks_attr__()
        rec = deepcopy(fields)
        rec.setdefault("created_at", _now())
        rec["runbook_key"] = rec.pop("runbook_key", rec.pop("rb_key", ""))
        rec["runbook_version"] = rec.pop("runbook_version", rec.pop("rb_version", 1))
        eid = rec["execution_id"]
        with self._lock:
            self.runbook_executions[eid] = rec
        return eid

    def list_runbook_executions(self, *, limit: int = 100, runbook_key: str | None = None) -> list[dict]:
        self.__init_runbooks_attr__()
        items = list(self.runbook_executions.values())
        if runbook_key:
            items = [x for x in items if x.get("runbook_key") == runbook_key]
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return [deepcopy(x) for x in items[:limit]]

    def get_runbook_execution(self, execution_id: str) -> dict | None:
        self.__init_runbooks_attr__()
        rec = self.runbook_executions.get(execution_id)
        return deepcopy(rec) if rec else None

    # ---- http skills ----
    def __init_http_skills_attr__(self) -> None:
        if not hasattr(self, "http_skills"):
            self.http_skills: dict[str, dict] = {}

    def list_http_skills(self) -> list[dict]:
        self.__init_http_skills_attr__()
        return [deepcopy(v) for v in self.http_skills.values()]

    def get_http_skill(self, code: str) -> dict | None:
        self.__init_http_skills_attr__()
        rec = self.http_skills.get(code)
        return deepcopy(rec) if rec else None

    def upsert_http_skill(self, *, code: str, title: str, description: str,
                          category: str, connection_id: str | None,
                          definition: dict, enabled: bool = True,
                          updated_by: str | None = None) -> dict:
        self.__init_http_skills_attr__()
        with self._lock:
            existing = self.http_skills.get(code) or {}
            rec = {
                "key": code,
                "code": code,
                "title": title,
                "description": description or "",
                "category": category or "integration",
                "connection_id": connection_id,
                "definition": deepcopy(definition or {}),
                "enabled": bool(enabled),
                "version": int(existing.get("version", 0)) + 1,
                "updated_by": updated_by,
                "updated_at": _now(),
            }
            self.http_skills[code] = rec
            return deepcopy(rec)

    def delete_http_skill(self, code: str) -> None:
        self.__init_http_skills_attr__()
        with self._lock:
            self.http_skills.pop(code, None)

    # ---- async tasks ----------------------------------------------------- #

    def __init_async_tasks_attr__(self) -> None:
        if not hasattr(self, "async_tasks"):
            self.async_tasks: dict[str, dict] = {}

    def create_async_task(self, **fields) -> dict:
        """新增异步任务记录。``task_id`` 必填且不能冲突。"""
        self.__init_async_tasks_attr__()
        with self._lock:
            task_id = fields["task_id"]
            if task_id in self.async_tasks:
                raise ValueError(f"async_task 主键冲突：{task_id}")
            now = _now()
            rec = {
                "task_id":              task_id,
                "connection_id":        fields["connection_id"],
                "connection_name":      fields.get("connection_name"),
                "node":                 fields["node"],
                "command":              fields["command"],
                "argv_json":            deepcopy(fields.get("argv") or []),
                "nsenter":              fields.get("nsenter"),
                "max_runtime_sec":      fields.get("max_runtime_sec"),
                "status":               fields.get("status", "submitting"),
                "exit_code":            None,
                "stdout":               "",
                "stderr":               "",
                "truncated":            False,
                "submitted_by":         fields.get("submitted_by"),
                "session_id":           fields.get("session_id"),
                "skill_call_id":        fields.get("skill_call_id"),
                "pending_action_token": fields.get("pending_action_token"),
                "submitted_at":         fields.get("submitted_at") or now,
                "started_at":           fields.get("started_at"),
                "ended_at":             None,
                "duration_ms":          None,
                "last_poll_at":         None,
                "last_poll_error":      None,
                "poll_count":           0,
                "created_at":           now,
                "updated_at":           now,
            }
            self.async_tasks[task_id] = rec
            return deepcopy(rec)

    def get_async_task(self, task_id: str) -> dict | None:
        self.__init_async_tasks_attr__()
        rec = self.async_tasks.get(task_id)
        return deepcopy(rec) if rec else None

    def update_async_task(self, task_id: str, **fields) -> dict | None:
        """部分字段更新。``task_id`` 不可改。"""
        self.__init_async_tasks_attr__()
        with self._lock:
            rec = self.async_tasks.get(task_id)
            if not rec:
                return None
            for k, v in fields.items():
                if k in {"task_id", "created_at"}:
                    continue
                rec[k] = v
            rec["updated_at"] = _now()
            return deepcopy(rec)

    def list_async_tasks(
        self,
        *,
        connection_id: str | None = None,
        node: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        submitted_by: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        self.__init_async_tasks_attr__()
        items = list(self.async_tasks.values())
        if connection_id:
            items = [t for t in items if t["connection_id"] == connection_id]
        if node:
            items = [t for t in items if t["node"] == node]
        if status:
            items = [t for t in items if t["status"] == status]
        if session_id:
            items = [t for t in items if t["session_id"] == session_id]
        if submitted_by:
            items = [t for t in items if t["submitted_by"] == submitted_by]
        items.sort(key=lambda t: t.get("submitted_at") or "", reverse=True)
        return [deepcopy(t) for t in items[:limit]]

    def list_running_async_tasks(self, *, limit: int = 500) -> list[dict]:
        """给后台 poller 用——一次性拿所有 running 任务。"""
        return self.list_async_tasks(status="running", limit=limit)


