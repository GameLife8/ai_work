from __future__ import annotations

import json
import logging
import secrets
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

# SQLAlchemy 是 requirements.txt 里的硬依赖；以前每个 SQLPlatformStore
# 方法都做 ``from sqlalchemy import text`` 局部 import，纯粹是冗余——
# 既然进程能 import 到 store.py 就一定能 import sqlalchemy，没什么好"懒"的。
# 全部上提到模块顶层避免重复绑定开销和 39 行噪音。
from sqlalchemy import text

from ops_platform.crypto import decrypt, encrypt, is_active as crypto_active


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


class SQLPlatformStore:
    """SQL 版本的平台存储；与 SQLStore 共用同一个 engine。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self._lock = threading.RLock()

    def initialize(self) -> None:
        is_sqlite = self.engine.dialect.name == "sqlite"
        statements = self._sqlite_ddl() if is_sqlite else self._mysql_ddl()
        with self.engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))

        # 增量 schema 迁移（幂等）：老库没有 ``tags_json`` 列就加上
        self._ensure_columns()

        # 启用加密时把存量明文行迁移成密文（幂等：已加密的不会重复加密）
        if crypto_active():
            try:
                self.migrate_encrypt_existing()
            except Exception as exc:  # pragma: no cover
                logger.warning("加密迁移跳过：%s", exc)

    def _ensure_columns(self) -> None:
        """对老数据库做幂等 column 增量。比 IF NOT EXISTS 通用——先 SELECT 探测，
        没列再 ADD COLUMN（MySQL 5.7/TiDB 不支持 ALTER TABLE IF NOT EXISTS）。"""
        with self.engine.begin() as conn:
            try:
                conn.execute(text("SELECT tags_json FROM platform_connection LIMIT 1"))
            except Exception:
                # 不存在；加列
                try:
                    is_sqlite = self.engine.dialect.name == "sqlite"
                    col_type = "TEXT" if is_sqlite else "LONGTEXT"
                    conn.execute(text(
                        f"ALTER TABLE platform_connection ADD COLUMN tags_json {col_type}"
                    ))
                    logger.info("迁移：platform_connection 加 tags_json 列完毕")
                except Exception as exc:    # pragma: no cover
                    logger.warning("加 tags_json 列失败（如已存在/老 DB 不支持）：%s", exc)

    def migrate_encrypt_existing(self) -> dict[str, int]:
        """把已存在但还是明文的 ``config_json`` / ``api_key`` 重新写成密文。

        通过 ``encrypt()`` 的"已加密则不重复"特性保持幂等；只把无前缀的
        rows 走一次 update 即可。
        """
        from ops_platform.crypto import is_encrypted, PREFIX

        stats = {"connections": 0, "model_configs": 0}
        with self.engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT id, config_json FROM platform_connection"
            )).mappings().all()
            for r in rows:
                cur = r.get("config_json")
                if cur and not is_encrypted(cur):
                    conn.execute(
                        text("UPDATE platform_connection SET config_json=:c WHERE id=:id"),
                        {"id": r["id"], "c": encrypt(cur)},
                    )
                    stats["connections"] += 1

            rows = conn.execute(text(
                "SELECT id, api_key FROM platform_model_config"
            )).mappings().all()
            for r in rows:
                cur = r.get("api_key")
                if cur and not is_encrypted(cur):
                    conn.execute(
                        text("UPDATE platform_model_config SET api_key=:k WHERE id=:id"),
                        {"id": r["id"], "k": encrypt(cur)},
                    )
                    stats["model_configs"] += 1

        if stats["connections"] or stats["model_configs"]:
            logger.info(
                "凭证加密迁移完成：%d 个 connection, %d 个 model_config",
                stats["connections"], stats["model_configs"],
            )
        return stats

    @staticmethod
    def _sqlite_ddl() -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS platform_user (
                id VARCHAR(64) PRIMARY KEY,
                username VARCHAR(128) NOT NULL UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                role VARCHAR(32) NOT NULL,
                display_name VARCHAR(128),
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_connection (
                id VARCHAR(64) PRIMARY KEY,
                type_code VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                alias VARCHAR(128),
                config_json TEXT,
                tags_json TEXT,
                is_default INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                status VARCHAR(32),
                created_by VARCHAR(128),
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_model_config (
                id VARCHAR(64) PRIMARY KEY,
                provider VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                base_url VARCHAR(512) NOT NULL,
                api_key VARCHAR(512),
                model VARCHAR(128),
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                is_default INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by VARCHAR(128),
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_skill_call (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_code VARCHAR(128) NOT NULL,
                connection_id VARCHAR(64),
                session_id VARCHAR(128),
                user VARCHAR(128),
                args_json TEXT,
                result_json TEXT,
                status VARCHAR(32),
                error TEXT,
                latency_ms INTEGER,
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_pending_action (
                token VARCHAR(64) PRIMARY KEY,
                skill_code VARCHAR(128) NOT NULL,
                args_json TEXT,
                connection_id VARCHAR(64),
                requires_admin_approval INTEGER NOT NULL DEFAULT 0,
                requested_by VARCHAR(128),
                session_id VARCHAR(128),
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                preview_json TEXT,
                created_at VARCHAR(64),
                expires_at VARCHAR(64),
                decided_at VARCHAR(64),
                decided_by VARCHAR(128),
                reject_reason TEXT,
                skill_call_id INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_prompt_segment (
                segment_key VARCHAR(64) PRIMARY KEY,
                title VARCHAR(255),
                content TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                updated_by VARCHAR(128),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_runbook (
                rb_key VARCHAR(128) PRIMARY KEY,
                title VARCHAR(255),
                description TEXT,
                triggers_json TEXT,
                inputs_json TEXT,
                definition_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                updated_by VARCHAR(128),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_runbook_execution (
                execution_id VARCHAR(64) PRIMARY KEY,
                rb_key VARCHAR(128) NOT NULL,
                rb_version INTEGER,
                triggered_by VARCHAR(128),
                session_id VARCHAR(128),
                user_inputs_json TEXT,
                global_status VARCHAR(32),
                abort_reason TEXT,
                total_ms INTEGER,
                node_states_json TEXT,
                signals_json TEXT,
                final_report TEXT,
                started_at VARCHAR(64),
                ended_at VARCHAR(64),
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_http_skill (
                skill_code VARCHAR(128) PRIMARY KEY,
                title VARCHAR(255),
                description TEXT,
                category VARCHAR(64),
                connection_id VARCHAR(64),
                definition_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                updated_by VARCHAR(128),
                updated_at VARCHAR(64)
            )
            """,
            # platform_async_task —— 持久化所有异步任务（agent 内存只是短期镜像）
            #
            # 关键设计：
            #   - DB 是 source of truth，agent 重启 / 30min GC 都不影响平台拿结果
            #   - task_id 由平台生成 PK，先写库再调 agent；agent 1.3+ 接受调用方传入的 task_id
            #   - 后台 poller 每 N 秒扫 status='running' 的任务，去 agent 拉最新状态写库
            #   - 终态(done/error/timeout/cancelled/lost)不再轮询，但记录永久保留供审计/回查
            """
            CREATE TABLE IF NOT EXISTS platform_async_task (
                task_id VARCHAR(64) PRIMARY KEY,
                connection_id VARCHAR(64) NOT NULL,
                connection_name VARCHAR(255),
                node VARCHAR(255) NOT NULL,
                command TEXT NOT NULL,
                argv_json TEXT,
                nsenter VARCHAR(32),
                max_runtime_sec INTEGER,
                status VARCHAR(32) NOT NULL,
                exit_code INTEGER,
                stdout TEXT,
                stderr TEXT,
                truncated INTEGER NOT NULL DEFAULT 0,
                submitted_by VARCHAR(128),
                session_id VARCHAR(128),
                skill_call_id INTEGER,
                pending_action_token VARCHAR(64),
                submitted_at VARCHAR(64),
                started_at VARCHAR(64),
                ended_at VARCHAR(64),
                duration_ms INTEGER,
                last_poll_at VARCHAR(64),
                last_poll_error TEXT,
                poll_count INTEGER NOT NULL DEFAULT 0,
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
        ]

    @staticmethod
    def _mysql_ddl() -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS platform_user (
                id VARCHAR(64) PRIMARY KEY,
                username VARCHAR(128) NOT NULL UNIQUE,
                password_hash VARCHAR(256) NOT NULL,
                role VARCHAR(32) NOT NULL,
                display_name VARCHAR(128),
                enabled TINYINT NOT NULL DEFAULT 1,
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_connection (
                id VARCHAR(64) PRIMARY KEY,
                type_code VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                alias VARCHAR(128),
                config_json LONGTEXT,
                tags_json LONGTEXT,
                is_default TINYINT NOT NULL DEFAULT 0,
                enabled TINYINT NOT NULL DEFAULT 1,
                status VARCHAR(32),
                created_by VARCHAR(128),
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_model_config (
                id VARCHAR(64) PRIMARY KEY,
                provider VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                base_url VARCHAR(512) NOT NULL,
                api_key VARCHAR(512),
                model VARCHAR(128),
                timeout_seconds INT NOT NULL DEFAULT 120,
                is_default TINYINT NOT NULL DEFAULT 0,
                enabled TINYINT NOT NULL DEFAULT 1,
                created_by VARCHAR(128),
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_skill_call (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                skill_code VARCHAR(128) NOT NULL,
                connection_id VARCHAR(64),
                session_id VARCHAR(128),
                user VARCHAR(128),
                args_json LONGTEXT,
                result_json LONGTEXT,
                status VARCHAR(32),
                error TEXT,
                latency_ms INT,
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_pending_action (
                token VARCHAR(64) PRIMARY KEY,
                skill_code VARCHAR(128) NOT NULL,
                args_json LONGTEXT,
                connection_id VARCHAR(64),
                requires_admin_approval TINYINT NOT NULL DEFAULT 0,
                requested_by VARCHAR(128),
                session_id VARCHAR(128),
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                preview_json LONGTEXT,
                created_at VARCHAR(64),
                expires_at VARCHAR(64),
                decided_at VARCHAR(64),
                decided_by VARCHAR(128),
                reject_reason TEXT,
                skill_call_id BIGINT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_prompt_segment (
                segment_key VARCHAR(64) PRIMARY KEY,
                title VARCHAR(255),
                content LONGTEXT,
                enabled TINYINT NOT NULL DEFAULT 1,
                version INT NOT NULL DEFAULT 1,
                updated_by VARCHAR(128),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_runbook (
                rb_key VARCHAR(128) PRIMARY KEY,
                title VARCHAR(255),
                description TEXT,
                triggers_json LONGTEXT,
                inputs_json LONGTEXT,
                definition_json LONGTEXT,
                enabled TINYINT NOT NULL DEFAULT 1,
                version INT NOT NULL DEFAULT 1,
                updated_by VARCHAR(128),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_runbook_execution (
                execution_id VARCHAR(64) PRIMARY KEY,
                rb_key VARCHAR(128) NOT NULL,
                rb_version INT,
                triggered_by VARCHAR(128),
                session_id VARCHAR(128),
                user_inputs_json LONGTEXT,
                global_status VARCHAR(32),
                abort_reason TEXT,
                total_ms INT,
                node_states_json LONGTEXT,
                signals_json LONGTEXT,
                final_report LONGTEXT,
                started_at VARCHAR(64),
                ended_at VARCHAR(64),
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_http_skill (
                skill_code VARCHAR(128) PRIMARY KEY,
                title VARCHAR(255),
                description TEXT,
                category VARCHAR(64),
                connection_id VARCHAR(64),
                definition_json LONGTEXT,
                enabled TINYINT NOT NULL DEFAULT 1,
                version INT NOT NULL DEFAULT 1,
                updated_by VARCHAR(128),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS platform_async_task (
                task_id VARCHAR(64) PRIMARY KEY,
                connection_id VARCHAR(64) NOT NULL,
                connection_name VARCHAR(255),
                node VARCHAR(255) NOT NULL,
                command LONGTEXT NOT NULL,
                argv_json LONGTEXT,
                nsenter VARCHAR(32),
                max_runtime_sec INT,
                status VARCHAR(32) NOT NULL,
                exit_code INT,
                stdout LONGTEXT,
                stderr LONGTEXT,
                truncated TINYINT NOT NULL DEFAULT 0,
                submitted_by VARCHAR(128),
                session_id VARCHAR(128),
                skill_call_id BIGINT,
                pending_action_token VARCHAR(64),
                submitted_at VARCHAR(64),
                started_at VARCHAR(64),
                ended_at VARCHAR(64),
                duration_ms INT,
                last_poll_at VARCHAR(64),
                last_poll_error TEXT,
                poll_count INT NOT NULL DEFAULT 0,
                created_at VARCHAR(64),
                updated_at VARCHAR(64),
                INDEX idx_async_task_status (status, updated_at),
                INDEX idx_async_task_session (session_id),
                INDEX idx_async_task_submitted_by (submitted_by),
                INDEX idx_async_task_node (connection_id, node)
            )
            """,
        ]

    # ---------- helpers ----------

    @staticmethod
    def _row_to_connection(row) -> dict:
        d = dict(row)
        # config_json 落库时整体加密；读出来先 decrypt 再 JSON.loads
        raw = decrypt(d.pop("config_json") or "{}")
        try:
            d["config"] = json.loads(raw or "{}")
        except json.JSONDecodeError:
            logger.warning("config_json 反序列化失败 (id=%s)，可能是加密数据用错了 key", d.get("id"))
            d["config"] = {}
        # tags_json 不加密（关键词不是机密信息，需要 LLM 看到）
        tags_raw = d.pop("tags_json", None)
        try:
            tags = json.loads(tags_raw) if tags_raw else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        d["tags"] = [str(t).strip() for t in tags if str(t).strip()] if isinstance(tags, list) else []
        d["is_default"] = bool(d.get("is_default"))
        d["enabled"] = bool(d.get("enabled", 1))
        return d

    @staticmethod
    def _row_to_model(row) -> dict:
        d = dict(row)
        if d.get("api_key"):
            d["api_key"] = decrypt(d["api_key"])
        d["is_default"] = bool(d.get("is_default"))
        d["enabled"] = bool(d.get("enabled", 1))
        return d

    @staticmethod
    def _row_to_user(row) -> dict:
        d = dict(row)
        d["enabled"] = bool(d.get("enabled", 1))
        return d

    # ---------- users ----------

    def list_users(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_user")).mappings().all()
        return [self._row_to_user(r) for r in rows]

    def get_user(self, user_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_user WHERE id=:id"),
                               {"id": user_id}).mappings().first()
        return self._row_to_user(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_user WHERE username=:u"),
                               {"u": username}).mappings().first()
        return self._row_to_user(row) if row else None

    def create_user(self, *, username: str, password_hash: str, role: str,
                    display_name: str = "") -> dict:
        now = _now()
        record = {
            "id": _new_id(),
            "username": username,
            "password_hash": password_hash,
            "role": role,
            "display_name": display_name or username,
            "enabled": 1,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO platform_user
                        (id, username, password_hash, role, display_name, enabled, created_at, updated_at)
                        VALUES (:id, :username, :password_hash, :role, :display_name, :enabled, :created_at, :updated_at)"""),
                record,
            )
        return self.get_user(record["id"])

    def update_user(self, user_id: str, **fields) -> dict:
        if not fields:
            return self.get_user(user_id)
        allowed = {"password_hash", "role", "display_name", "enabled"}
        sets = []
        params = {"id": user_id, "updated_at": _now()}
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=:{k}")
            params[k] = int(v) if k == "enabled" else v
        sets.append("updated_at=:updated_at")
        with self.engine.begin() as conn:
            conn.execute(text(f"UPDATE platform_user SET {', '.join(sets)} WHERE id=:id"), params)
        return self.get_user(user_id)

    def delete_user(self, user_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_user WHERE id=:id"), {"id": user_id})

    # ---------- connections ----------

    def list_connections(self, type_code: str | None = None) -> list[dict]:
        sql = "SELECT * FROM platform_connection"
        params: dict[str, Any] = {}
        if type_code:
            sql += " WHERE type_code=:t"
            params["t"] = type_code
        sql += " ORDER BY created_at"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [self._row_to_connection(r) for r in rows]

    def get_connection(self, connection_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_connection WHERE id=:id"),
                               {"id": connection_id}).mappings().first()
        return self._row_to_connection(row) if row else None

    def create_connection(self, *, type_code: str, name: str, alias: str,
                          config: dict, is_default: bool, created_by: str | None,
                          tags: list[str] | None = None) -> dict:
        now = _now()
        clean_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        with self._lock, self.engine.begin() as conn:
            if is_default:
                conn.execute(text("UPDATE platform_connection SET is_default=0 WHERE type_code=:t"),
                             {"t": type_code})
            record = {
                "id": _new_id(),
                "type_code": type_code,
                "name": name,
                "alias": alias or "",
                "config_json": encrypt(json.dumps(config, ensure_ascii=False)),
                "tags_json": json.dumps(clean_tags, ensure_ascii=False),
                "is_default": 1 if is_default else 0,
                "enabled": 1,
                "status": "unknown",
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                text("""INSERT INTO platform_connection
                        (id, type_code, name, alias, config_json, tags_json, is_default, enabled, status, created_by, created_at, updated_at)
                        VALUES (:id, :type_code, :name, :alias, :config_json, :tags_json, :is_default, :enabled, :status, :created_by, :created_at, :updated_at)"""),
                record,
            )
        return self.get_connection(record["id"])

    def update_connection(self, connection_id: str, **fields) -> dict:
        if not fields:
            return self.get_connection(connection_id)
        allowed = {"name", "alias", "config", "tags", "is_default", "enabled", "status"}
        sets = []
        params: dict[str, Any] = {"id": connection_id, "updated_at": _now()}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "config":
                sets.append("config_json=:config_json")
                params["config_json"] = encrypt(json.dumps(v, ensure_ascii=False))
            elif k == "tags":
                clean = [str(t).strip() for t in (v or []) if str(t).strip()]
                sets.append("tags_json=:tags_json")
                params["tags_json"] = json.dumps(clean, ensure_ascii=False)
            elif k in {"is_default", "enabled"}:
                sets.append(f"{k}=:{k}")
                params[k] = 1 if v else 0
            else:
                sets.append(f"{k}=:{k}")
                params[k] = v
        sets.append("updated_at=:updated_at")
        with self._lock, self.engine.begin() as conn:
            if fields.get("is_default"):
                row = conn.execute(text("SELECT type_code FROM platform_connection WHERE id=:id"),
                                   {"id": connection_id}).mappings().first()
                if row:
                    conn.execute(
                        text("UPDATE platform_connection SET is_default=0 WHERE type_code=:t AND id<>:id"),
                        {"t": row["type_code"], "id": connection_id},
                    )
            conn.execute(text(f"UPDATE platform_connection SET {', '.join(sets)} WHERE id=:id"), params)
        return self.get_connection(connection_id)

    def delete_connection(self, connection_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_connection WHERE id=:id"), {"id": connection_id})

    # ---------- model_configs ----------

    def list_model_configs(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_model_config ORDER BY created_at")).mappings().all()
        return [self._row_to_model(r) for r in rows]

    def get_model_config(self, model_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_model_config WHERE id=:id"),
                               {"id": model_id}).mappings().first()
        return self._row_to_model(row) if row else None

    def create_model_config(self, *, provider: str, name: str, base_url: str,
                            api_key: str, model: str, is_default: bool,
                            created_by: str | None) -> dict:
        now = _now()
        with self._lock, self.engine.begin() as conn:
            if is_default:
                conn.execute(text("UPDATE platform_model_config SET is_default=0"))
            record = {
                "id": _new_id(),
                "provider": provider,
                "name": name,
                "base_url": base_url,
                "api_key": encrypt(api_key) if api_key else api_key,
                "model": model,
                "timeout_seconds": 120,
                "is_default": 1 if is_default else 0,
                "enabled": 1,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                text("""INSERT INTO platform_model_config
                        (id, provider, name, base_url, api_key, model, timeout_seconds, is_default, enabled, created_by, created_at, updated_at)
                        VALUES (:id, :provider, :name, :base_url, :api_key, :model, :timeout_seconds, :is_default, :enabled, :created_by, :created_at, :updated_at)"""),
                record,
            )
        return self.get_model_config(record["id"])

    def update_model_config(self, model_id: str, **fields) -> dict:
        if not fields:
            return self.get_model_config(model_id)
        allowed = {"provider", "name", "base_url", "api_key", "model",
                   "timeout_seconds", "is_default", "enabled"}
        sets, params = [], {"id": model_id, "updated_at": _now()}
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=:{k}")
            if k in {"is_default", "enabled"}:
                params[k] = 1 if v else 0
            elif k == "api_key":
                params[k] = encrypt(v) if v else v
            else:
                params[k] = v
        sets.append("updated_at=:updated_at")
        with self._lock, self.engine.begin() as conn:
            if fields.get("is_default"):
                conn.execute(text("UPDATE platform_model_config SET is_default=0 WHERE id<>:id"),
                             {"id": model_id})
            conn.execute(text(f"UPDATE platform_model_config SET {', '.join(sets)} WHERE id=:id"), params)
        return self.get_model_config(model_id)

    def delete_model_config(self, model_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_model_config WHERE id=:id"), {"id": model_id})

    # ---------- skill_call audit ----------

    def save_skill_call(self, **fields) -> int:
        record = {
            "skill_code": fields.get("skill_code"),
            "connection_id": fields.get("connection_id"),
            "session_id": fields.get("session_id"),
            "user": fields.get("user"),
            "args_json": json.dumps(fields.get("args") or {}, ensure_ascii=False),
            "result_json": json.dumps(fields.get("result") or {}, ensure_ascii=False),
            "status": fields.get("status", "ok"),
            "error": fields.get("error"),
            "latency_ms": fields.get("latency_ms"),
            "created_at": _now(),
        }
        with self.engine.begin() as conn:
            result = conn.execute(
                text("""INSERT INTO platform_skill_call
                        (skill_code, connection_id, session_id, user, args_json, result_json, status, error, latency_ms, created_at)
                        VALUES (:skill_code, :connection_id, :session_id, :user, :args_json, :result_json, :status, :error, :latency_ms, :created_at)"""),
                record,
            )
            return int(result.lastrowid)

    def list_skill_calls(self, *, limit: int = 100) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text("SELECT * FROM platform_skill_call ORDER BY id DESC LIMIT :n"),
                {"n": limit},
            ).mappings().all()
        return [dict(r) for r in rows]

    # ---------- pending actions ----------

    @staticmethod
    def _row_to_pending(row) -> dict:
        d = dict(row)
        d["args_json"] = json.loads(d.get("args_json") or "{}")
        d["preview_json"] = json.loads(d.get("preview_json") or "{}")
        d["requires_admin_approval"] = bool(d.get("requires_admin_approval"))
        return d

    def create_pending_action(self, *, token: str, skill_code: str, args: dict,
                              connection_id: str | None, requires_admin_approval: bool,
                              requested_by: str | None, session_id: str | None,
                              expires_at: str, preview: dict) -> dict:
        record = {
            "token": token,
            "skill_code": skill_code,
            "args_json": json.dumps(args, ensure_ascii=False),
            "connection_id": connection_id,
            "requires_admin_approval": 1 if requires_admin_approval else 0,
            "requested_by": requested_by,
            "session_id": session_id,
            "status": "pending",
            "preview_json": json.dumps(preview, ensure_ascii=False),
            "created_at": _now(),
            "expires_at": expires_at,
            "decided_at": None,
            "decided_by": None,
            "reject_reason": None,
            "skill_call_id": None,
        }
        with self.engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO platform_pending_action
                    (token, skill_code, args_json, connection_id, requires_admin_approval,
                     requested_by, session_id, status, preview_json, created_at, expires_at,
                     decided_at, decided_by, reject_reason, skill_call_id)
                    VALUES (:token, :skill_code, :args_json, :connection_id, :requires_admin_approval,
                            :requested_by, :session_id, :status, :preview_json, :created_at, :expires_at,
                            :decided_at, :decided_by, :reject_reason, :skill_call_id)"""),
                record,
            )
        return self.get_pending_action(token)

    def get_pending_action(self, token: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM platform_pending_action WHERE token=:t"),
                {"t": token},
            ).mappings().first()
        return self._row_to_pending(row) if row else None

    def list_pending_actions(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM platform_pending_action"
        params: dict[str, Any] = {"n": limit}
        if status:
            sql += " WHERE status=:s"
            params["s"] = status
        sql += " ORDER BY created_at DESC LIMIT :n"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [self._row_to_pending(r) for r in rows]

    # ---------- prompt segments ----------

    @staticmethod
    def _row_to_segment(row) -> dict:
        d = dict(row)
        # 数据库列名是 segment_key（key 是 SQL 保留字）；对外字段统一叫 key
        if "segment_key" in d:
            d["key"] = d.pop("segment_key")
        d["enabled"] = bool(d.get("enabled", 1))
        d["version"] = int(d.get("version") or 1)
        return d

    def list_prompt_segments(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_prompt_segment")).mappings().all()
        return [self._row_to_segment(r) for r in rows]

    def get_prompt_segment(self, key: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM platform_prompt_segment WHERE segment_key=:k"),
                {"k": key},
            ).mappings().first()
        return self._row_to_segment(row) if row else None

    def upsert_prompt_segment(self, *, key: str, title: str, content: str,
                              enabled: bool = True, updated_by: str | None = None) -> dict:
        now = _now()
        with self.engine.begin() as conn:
            existing = conn.execute(
                text("SELECT version FROM platform_prompt_segment WHERE segment_key=:k"),
                {"k": key},
            ).mappings().first()
            params = {
                "k": key,
                "title": title,
                "content": content,
                "enabled": 1 if enabled else 0,
                "updated_by": updated_by,
                "updated_at": now,
            }
            if existing:
                params["version"] = int(existing["version"] or 1) + 1
                conn.execute(
                    text("""UPDATE platform_prompt_segment
                            SET title=:title, content=:content, enabled=:enabled,
                                version=:version, updated_by=:updated_by, updated_at=:updated_at
                            WHERE segment_key=:k"""),
                    params,
                )
            else:
                params["version"] = 1
                conn.execute(
                    text("""INSERT INTO platform_prompt_segment
                            (segment_key, title, content, enabled, version, updated_by, updated_at)
                            VALUES (:k, :title, :content, :enabled, :version, :updated_by, :updated_at)"""),
                    params,
                )
        return self.get_prompt_segment(key)

    def delete_prompt_segment(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_prompt_segment WHERE segment_key=:k"), {"k": key})

    # ---------- runbooks ----------

    @staticmethod
    def _row_to_runbook(row) -> dict:
        d = dict(row)
        if "rb_key" in d:
            d["key"] = d.pop("rb_key")
        d["triggers"] = json.loads(d.pop("triggers_json") or "[]")
        d["inputs"] = json.loads(d.pop("inputs_json") or "[]")
        d["definition"] = json.loads(d.pop("definition_json") or "{}")
        d["enabled"] = bool(d.get("enabled", 1))
        d["version"] = int(d.get("version") or 1)
        return d

    def list_runbooks(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_runbook ORDER BY rb_key")).mappings().all()
        return [self._row_to_runbook(r) for r in rows]

    def get_runbook(self, key: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_runbook WHERE rb_key=:k"),
                               {"k": key}).mappings().first()
        return self._row_to_runbook(row) if row else None

    def upsert_runbook(self, *, key: str, title: str, description: str,
                       triggers: list, inputs: list, definition: dict,
                       enabled: bool = True, updated_by: str | None = None) -> dict:
        now = _now()
        with self.engine.begin() as conn:
            existing = conn.execute(
                text("SELECT version FROM platform_runbook WHERE rb_key=:k"),
                {"k": key},
            ).mappings().first()
            params = {
                "k": key,
                "title": title,
                "description": description or "",
                "triggers_json": json.dumps(triggers or [], ensure_ascii=False),
                "inputs_json": json.dumps(inputs or [], ensure_ascii=False),
                "definition_json": json.dumps(definition or {}, ensure_ascii=False),
                "enabled": 1 if enabled else 0,
                "updated_by": updated_by,
                "updated_at": now,
            }
            if existing:
                params["version"] = int(existing["version"] or 1) + 1
                conn.execute(
                    text("""UPDATE platform_runbook
                            SET title=:title, description=:description,
                                triggers_json=:triggers_json, inputs_json=:inputs_json,
                                definition_json=:definition_json, enabled=:enabled,
                                version=:version, updated_by=:updated_by, updated_at=:updated_at
                            WHERE rb_key=:k"""),
                    params,
                )
            else:
                params["version"] = 1
                conn.execute(
                    text("""INSERT INTO platform_runbook
                            (rb_key, title, description, triggers_json, inputs_json,
                             definition_json, enabled, version, updated_by, updated_at)
                            VALUES (:k, :title, :description, :triggers_json, :inputs_json,
                                    :definition_json, :enabled, :version, :updated_by, :updated_at)"""),
                    params,
                )
        return self.get_runbook(key)

    def delete_runbook(self, key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_runbook WHERE rb_key=:k"), {"k": key})

    # ---------- runbook executions ----------

    @staticmethod
    def _row_to_execution(row) -> dict:
        d = dict(row)
        if "rb_key" in d:
            d["runbook_key"] = d.pop("rb_key")
        if "rb_version" in d:
            d["runbook_version"] = d.pop("rb_version")
        for col in ("user_inputs_json", "node_states_json", "signals_json"):
            if col in d:
                fld = col[:-5] if col.endswith("_json") else col
                d[fld] = json.loads(d.pop(col) or "null")
        return d

    def save_runbook_execution(
        self,
        *,
        execution_id: str,
        runbook_key: str,
        runbook_version: int,
        triggered_by: str | None,
        session_id: str | None,
        user_inputs: dict | None,
        global_status: str,
        abort_reason: str | None,
        total_ms: int,
        node_states: list,
        signals: list,
        final_report: str = "",
        started_at: str = "",
        ended_at: str = "",
    ) -> str:
        record = {
            "execution_id": execution_id,
            "rb_key": runbook_key,
            "rb_version": runbook_version,
            "triggered_by": triggered_by,
            "session_id": session_id,
            "user_inputs_json": json.dumps(user_inputs or {}, ensure_ascii=False, default=str),
            "global_status": global_status,
            "abort_reason": abort_reason,
            "total_ms": total_ms,
            "node_states_json": json.dumps(node_states or [], ensure_ascii=False, default=str),
            "signals_json": json.dumps(signals or [], ensure_ascii=False, default=str),
            "final_report": final_report,
            "started_at": started_at,
            "ended_at": ended_at,
            "created_at": _now(),
        }
        with self.engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO platform_runbook_execution
                        (execution_id, rb_key, rb_version, triggered_by, session_id,
                         user_inputs_json, global_status, abort_reason, total_ms,
                         node_states_json, signals_json, final_report,
                         started_at, ended_at, created_at)
                        VALUES (:execution_id, :rb_key, :rb_version, :triggered_by, :session_id,
                                :user_inputs_json, :global_status, :abort_reason, :total_ms,
                                :node_states_json, :signals_json, :final_report,
                                :started_at, :ended_at, :created_at)"""),
                record,
            )
        return execution_id

    def list_runbook_executions(self, *, limit: int = 100, runbook_key: str | None = None) -> list[dict]:
        sql = "SELECT * FROM platform_runbook_execution"
        params: dict[str, Any] = {"n": limit}
        if runbook_key:
            sql += " WHERE rb_key=:k"
            params["k"] = runbook_key
        sql += " ORDER BY created_at DESC LIMIT :n"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [self._row_to_execution(r) for r in rows]

    def get_runbook_execution(self, execution_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM platform_runbook_execution WHERE execution_id=:id"),
                {"id": execution_id},
            ).mappings().first()
        return self._row_to_execution(row) if row else None

    # ---------- http skills ----------

    @staticmethod
    def _row_to_http_skill(row) -> dict:
        d = dict(row)
        if "skill_code" in d:
            d["key"] = d["skill_code"]
            d["code"] = d.pop("skill_code")
        d["definition"] = json.loads(d.pop("definition_json") or "{}")
        d["enabled"] = bool(d.get("enabled", 1))
        d["version"] = int(d.get("version") or 1)
        return d

    def list_http_skills(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_http_skill ORDER BY skill_code")).mappings().all()
        return [self._row_to_http_skill(r) for r in rows]

    def get_http_skill(self, code: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM platform_http_skill WHERE skill_code=:c"),
                {"c": code},
            ).mappings().first()
        return self._row_to_http_skill(row) if row else None

    def upsert_http_skill(self, *, code: str, title: str, description: str,
                          category: str, connection_id: str | None,
                          definition: dict, enabled: bool = True,
                          updated_by: str | None = None) -> dict:
        now = _now()
        with self.engine.begin() as conn:
            existing = conn.execute(
                text("SELECT version FROM platform_http_skill WHERE skill_code=:c"),
                {"c": code},
            ).mappings().first()
            params = {
                "c": code,
                "title": title,
                "description": description or "",
                "category": category or "integration",
                "connection_id": connection_id,
                "definition_json": json.dumps(definition or {}, ensure_ascii=False),
                "enabled": 1 if enabled else 0,
                "updated_by": updated_by,
                "updated_at": now,
            }
            if existing:
                params["version"] = int(existing["version"] or 1) + 1
                conn.execute(
                    text("""UPDATE platform_http_skill
                            SET title=:title, description=:description, category=:category,
                                connection_id=:connection_id, definition_json=:definition_json,
                                enabled=:enabled, version=:version,
                                updated_by=:updated_by, updated_at=:updated_at
                            WHERE skill_code=:c"""),
                    params,
                )
            else:
                params["version"] = 1
                conn.execute(
                    text("""INSERT INTO platform_http_skill
                            (skill_code, title, description, category, connection_id,
                             definition_json, enabled, version, updated_by, updated_at)
                            VALUES (:c, :title, :description, :category, :connection_id,
                                    :definition_json, :enabled, :version, :updated_by, :updated_at)"""),
                    params,
                )
        return self.get_http_skill(code)

    def delete_http_skill(self, code: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_http_skill WHERE skill_code=:c"), {"c": code})

    def update_pending_action_status(self, token: str, *, status: str,
                                     decided_by: str | None = None,
                                     reject_reason: str | None = None,
                                     skill_call_id: int | None = None) -> dict | None:
        sets = ["status=:status", "decided_at=:decided_at"]
        params: dict[str, Any] = {"token": token, "status": status, "decided_at": _now()}
        if decided_by is not None:
            sets.append("decided_by=:decided_by"); params["decided_by"] = decided_by
        if reject_reason is not None:
            sets.append("reject_reason=:reject_reason"); params["reject_reason"] = reject_reason
        if skill_call_id is not None:
            sets.append("skill_call_id=:skill_call_id"); params["skill_call_id"] = skill_call_id
        with self.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE platform_pending_action SET {', '.join(sets)} WHERE token=:token"),
                params,
            )
        return self.get_pending_action(token)

    # ---------- async tasks ---------- #

    _ASYNC_TASK_UPDATABLE = {
        "connection_name", "status", "exit_code", "stdout", "stderr", "truncated",
        "started_at", "ended_at", "duration_ms", "last_poll_at", "last_poll_error",
        "poll_count", "skill_call_id", "pending_action_token",
    }

    @staticmethod
    def _row_to_async_task(row) -> dict:
        d = dict(row)
        # argv_json 落库时序列化；读出来反序列化
        if d.get("argv_json"):
            try:
                d["argv_json"] = json.loads(d["argv_json"])
            except (ValueError, json.JSONDecodeError):
                pass
        d["truncated"] = bool(d.get("truncated") or 0)
        return d

    def create_async_task(self, **fields) -> dict:
        now = _now()
        record = {
            "task_id":              fields["task_id"],
            "connection_id":        fields["connection_id"],
            "connection_name":      fields.get("connection_name"),
            "node":                 fields["node"],
            "command":              fields["command"],
            "argv_json":            json.dumps(fields.get("argv") or [], ensure_ascii=False),
            "nsenter":              fields.get("nsenter"),
            "max_runtime_sec":      fields.get("max_runtime_sec"),
            "status":               fields.get("status", "submitting"),
            "exit_code":            None,
            "stdout":               "",
            "stderr":               "",
            "truncated":            0,
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
        with self.engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO platform_async_task
                        (task_id, connection_id, connection_name, node, command, argv_json,
                         nsenter, max_runtime_sec, status, exit_code, stdout, stderr, truncated,
                         submitted_by, session_id, skill_call_id, pending_action_token,
                         submitted_at, started_at, ended_at, duration_ms,
                         last_poll_at, last_poll_error, poll_count, created_at, updated_at)
                        VALUES (:task_id, :connection_id, :connection_name, :node, :command, :argv_json,
                                :nsenter, :max_runtime_sec, :status, :exit_code, :stdout, :stderr, :truncated,
                                :submitted_by, :session_id, :skill_call_id, :pending_action_token,
                                :submitted_at, :started_at, :ended_at, :duration_ms,
                                :last_poll_at, :last_poll_error, :poll_count, :created_at, :updated_at)"""),
                record,
            )
        return self.get_async_task(record["task_id"])

    def get_async_task(self, task_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM platform_async_task WHERE task_id=:t"),
                {"t": task_id},
            ).mappings().first()
        return self._row_to_async_task(row) if row else None

    def update_async_task(self, task_id: str, **fields) -> dict | None:
        if not fields:
            return self.get_async_task(task_id)
        sets, params = [], {"t": task_id, "updated_at": _now()}
        for k, v in fields.items():
            if k not in self._ASYNC_TASK_UPDATABLE:
                continue
            sets.append(f"{k}=:{k}")
            if k == "truncated":
                params[k] = 1 if v else 0
            else:
                params[k] = v
        if not sets:
            return self.get_async_task(task_id)
        sets.append("updated_at=:updated_at")
        with self.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE platform_async_task SET {', '.join(sets)} WHERE task_id=:t"),
                params,
            )
        return self.get_async_task(task_id)

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
        sql = "SELECT * FROM platform_async_task"
        clauses, params = [], {}
        if connection_id:
            clauses.append("connection_id=:cid"); params["cid"] = connection_id
        if node:
            clauses.append("node=:nd"); params["nd"] = node
        if status:
            clauses.append("status=:st"); params["st"] = status
        if session_id:
            clauses.append("session_id=:sid"); params["sid"] = session_id
        if submitted_by:
            clauses.append("submitted_by=:sb"); params["sb"] = submitted_by
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY submitted_at DESC LIMIT :limit"
        params["limit"] = int(limit)
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [self._row_to_async_task(r) for r in rows]

    def list_running_async_tasks(self, *, limit: int = 500) -> list[dict]:
        return self.list_async_tasks(status="running", limit=limit)


def attach_platform_store(store: Any) -> Any:
    """根据已有 store 类型创建对应的 platform store，并把方法 bind 到原 store。"""
    if hasattr(store, "engine"):
        platform = SQLPlatformStore(store.engine)
        platform.initialize()
    else:
        platform = InMemoryPlatformStore()

    method_names = [
        "list_users", "get_user", "get_user_by_username", "create_user", "update_user", "delete_user",
        "list_connections", "get_connection", "create_connection", "update_connection", "delete_connection",
        "list_model_configs", "get_model_config", "create_model_config", "update_model_config", "delete_model_config",
        "save_skill_call", "list_skill_calls",
        "create_pending_action", "get_pending_action", "list_pending_actions", "update_pending_action_status",
        "list_prompt_segments", "get_prompt_segment", "upsert_prompt_segment", "delete_prompt_segment",
        "list_runbooks", "get_runbook", "upsert_runbook", "delete_runbook",
        "save_runbook_execution", "list_runbook_executions", "get_runbook_execution",
        "list_http_skills", "get_http_skill", "upsert_http_skill", "delete_http_skill",
        "create_async_task", "get_async_task", "update_async_task",
        "list_async_tasks", "list_running_async_tasks",
    ]
    for name in method_names:
        setattr(store, name, getattr(platform, name))
    store.platform = platform
    return store
