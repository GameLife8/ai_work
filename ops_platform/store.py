from __future__ import annotations

import json
import logging
import secrets
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

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
        return [deepcopy(c) for c in items]

    def get_connection(self, connection_id: str) -> dict | None:
        return next((deepcopy(c) for c in self.connections if c["id"] == connection_id), None)

    def create_connection(self, *, type_code: str, name: str, alias: str,
                          config: dict, is_default: bool, created_by: str | None) -> dict:
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


class SQLPlatformStore:
    """SQL 版本的平台存储；与 SQLStore 共用同一个 engine。"""

    def __init__(self, engine) -> None:
        self.engine = engine
        self._lock = threading.RLock()

    def initialize(self) -> None:
        from sqlalchemy import text

        is_sqlite = self.engine.dialect.name == "sqlite"
        statements = self._sqlite_ddl() if is_sqlite else self._mysql_ddl()
        with self.engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))

        # 启用加密时把存量明文行迁移成密文（幂等：已加密的不会重复加密）
        if crypto_active():
            try:
                self.migrate_encrypt_existing()
            except Exception as exc:  # pragma: no cover
                logger.warning("加密迁移跳过：%s", exc)

    def migrate_encrypt_existing(self) -> dict[str, int]:
        """把已存在但还是明文的 ``config_json`` / ``api_key`` 重新写成密文。

        通过 ``encrypt()`` 的"已加密则不重复"特性保持幂等；只把无前缀的
        rows 走一次 update 即可。
        """
        from sqlalchemy import text

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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_user")).mappings().all()
        return [self._row_to_user(r) for r in rows]

    def get_user(self, user_id: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_user WHERE id=:id"),
                               {"id": user_id}).mappings().first()
        return self._row_to_user(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_user WHERE username=:u"),
                               {"u": username}).mappings().first()
        return self._row_to_user(row) if row else None

    def create_user(self, *, username: str, password_hash: str, role: str,
                    display_name: str = "") -> dict:
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_user WHERE id=:id"), {"id": user_id})

    # ---------- connections ----------

    def list_connections(self, type_code: str | None = None) -> list[dict]:
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_connection WHERE id=:id"),
                               {"id": connection_id}).mappings().first()
        return self._row_to_connection(row) if row else None

    def create_connection(self, *, type_code: str, name: str, alias: str,
                          config: dict, is_default: bool, created_by: str | None) -> dict:
        from sqlalchemy import text
        now = _now()
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
                "is_default": 1 if is_default else 0,
                "enabled": 1,
                "status": "unknown",
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                text("""INSERT INTO platform_connection
                        (id, type_code, name, alias, config_json, is_default, enabled, status, created_by, created_at, updated_at)
                        VALUES (:id, :type_code, :name, :alias, :config_json, :is_default, :enabled, :status, :created_by, :created_at, :updated_at)"""),
                record,
            )
        return self.get_connection(record["id"])

    def update_connection(self, connection_id: str, **fields) -> dict:
        from sqlalchemy import text
        if not fields:
            return self.get_connection(connection_id)
        allowed = {"name", "alias", "config", "is_default", "enabled", "status"}
        sets = []
        params: dict[str, Any] = {"id": connection_id, "updated_at": _now()}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "config":
                sets.append("config_json=:config_json")
                params["config_json"] = encrypt(json.dumps(v, ensure_ascii=False))
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_connection WHERE id=:id"), {"id": connection_id})

    # ---------- model_configs ----------

    def list_model_configs(self) -> list[dict]:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_model_config ORDER BY created_at")).mappings().all()
        return [self._row_to_model(r) for r in rows]

    def get_model_config(self, model_id: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_model_config WHERE id=:id"),
                               {"id": model_id}).mappings().first()
        return self._row_to_model(row) if row else None

    def create_model_config(self, *, provider: str, name: str, base_url: str,
                            api_key: str, model: str, is_default: bool,
                            created_by: str | None) -> dict:
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_model_config WHERE id=:id"), {"id": model_id})

    # ---------- skill_call audit ----------

    def save_skill_call(self, **fields) -> int:
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM platform_pending_action WHERE token=:t"),
                {"t": token},
            ).mappings().first()
        return self._row_to_pending(row) if row else None

    def list_pending_actions(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_prompt_segment")).mappings().all()
        return [self._row_to_segment(r) for r in rows]

    def get_prompt_segment(self, key: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM platform_prompt_segment WHERE segment_key=:k"),
                {"k": key},
            ).mappings().first()
        return self._row_to_segment(row) if row else None

    def upsert_prompt_segment(self, *, key: str, title: str, content: str,
                              enabled: bool = True, updated_by: str | None = None) -> dict:
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_runbook ORDER BY rb_key")).mappings().all()
        return [self._row_to_runbook(r) for r in rows]

    def get_runbook(self, key: str) -> dict | None:
        from sqlalchemy import text
        with self.engine.begin() as conn:
            row = conn.execute(text("SELECT * FROM platform_runbook WHERE rb_key=:k"),
                               {"k": key}).mappings().first()
        return self._row_to_runbook(row) if row else None

    def upsert_runbook(self, *, key: str, title: str, description: str,
                       triggers: list, inputs: list, definition: dict,
                       enabled: bool = True, updated_by: str | None = None) -> dict:
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT * FROM platform_http_skill ORDER BY skill_code")).mappings().all()
        return [self._row_to_http_skill(r) for r in rows]

    def get_http_skill(self, code: str) -> dict | None:
        from sqlalchemy import text
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
        from sqlalchemy import text
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
        from sqlalchemy import text
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM platform_http_skill WHERE skill_code=:c"), {"c": code})

    def update_pending_action_status(self, token: str, *, status: str,
                                     decided_by: str | None = None,
                                     reject_reason: str | None = None,
                                     skill_call_id: int | None = None) -> dict | None:
        from sqlalchemy import text
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
    ]
    for name in method_names:
        setattr(store, name, getattr(platform, name))
    store.platform = platform
    return store
