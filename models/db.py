from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


logger = logging.getLogger(__name__)


def _alert_reference_time(alert: dict) -> datetime:
    raw = str(alert.get("event_time", "")).strip()
    if not raw:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return datetime.now(UTC)


def _alert_reference_time_iso(alert: dict) -> str:
    return _alert_reference_time(alert).isoformat()


class InMemoryStore:
    def __init__(self) -> None:
        self.alert_events: list[dict] = []
        self.alert_decisions: list[dict] = []
        self.incidents: dict[str, dict] = {}
        self.incident_alert_rels: list[dict] = []
        self.chat_sessions: dict[str, dict] = {}
        self.chat_messages: list[dict] = []

    def save_alert_event(self, alert: dict, raw_payload: dict) -> int:
        alert_event_id = len(self.alert_events) + 1
        record = deepcopy(alert)
        record["id"] = alert_event_id
        record["dedup_key"] = self.build_dedup_key(alert)
        record["raw_payload_json"] = deepcopy(raw_payload)
        record["created_at"] = _utc_now_iso()
        self.alert_events.append(record)
        return alert_event_id

    @property
    def backend_name(self) -> str:
        return "memory"

    def healthcheck(self) -> dict:
        return {
            "backend": self.backend_name,
            "healthy": True,
            "details": {
                "mode": "in-memory",
                "alert_events": len(self.alert_events),
                "alert_decisions": len(self.alert_decisions),
                "incidents": len(self.incidents),
                "chat_sessions": len(self.chat_sessions),
                "chat_messages": len(self.chat_messages),
            },
        }

    def save_alert_decision(self, alert_event_id: int, decision: dict, plan: dict | None = None, context: dict | None = None) -> int:
        decision_id = len(self.alert_decisions) + 1
        record = deepcopy(decision)
        record["id"] = decision_id
        record["alert_event_id"] = alert_event_id
        record["ai_plan"] = deepcopy(plan or {})
        record["context"] = deepcopy(context or {})
        record["created_at"] = _utc_now_iso()
        self.alert_decisions.append(record)
        return decision_id

    def get_related_incidents(self, alert: dict) -> list[dict]:
        dedup_key = self.build_dedup_key(alert)
        host_name = alert.get("host_name")
        service = alert.get("tags", {}).get("service")
        related = []
        for incident in self.incidents.values():
            if incident["status"] != "open":
                continue
            if incident.get("dedup_key") == dedup_key or incident.get("host_name") == host_name or incident.get("service") == service:
                related.append(
                    {
                        "incident_no": incident["incident_no"],
                        "status": incident["status"],
                        "priority": incident["priority"],
                        "ticket_status": incident.get("ticket_status"),
                    }
                )
        return related

    def incident_exists(self, incident_no: str) -> bool:
        return incident_no in self.incidents

    def get_incident(self, incident_no: str) -> dict | None:
        incident = self.incidents.get(incident_no)
        return deepcopy(incident) if incident else None

    def get_open_incident_by_alert(self, alert: dict) -> dict | None:
        dedup_key = self.build_dedup_key(alert)
        incident = next(
            (item for item in self.incidents.values() if item["status"] == "open" and item.get("dedup_key") == dedup_key),
            None,
        )
        return deepcopy(incident) if incident else None

    def touch_incident(self, incident_no: str, seen_at: str | None = None) -> None:
        if incident_no in self.incidents:
            self.incidents[incident_no]["last_seen_at"] = seen_at or _utc_now_iso()
            self.incidents[incident_no]["updated_at"] = _utc_now_iso()

    def create_or_update_incident(self, alert_event_id: int, alert: dict, decision: dict) -> dict:
        root_key = self._build_root_key(alert)
        dedup_key = self.build_dedup_key(alert)
        existing = next(
            (item for item in self.incidents.values() if item["status"] == "open" and item.get("dedup_key") == dedup_key),
            None,
        )
        now = _utc_now_iso()
        seen_at = _alert_reference_time_iso(alert)

        if existing:
            existing["last_seen_at"] = seen_at
            existing["updated_at"] = now
            existing["priority"] = decision.get("priority")
            existing["summary"] = alert.get("alert_name")
            existing["last_decision_type"] = decision.get("decision")
            existing["last_reason"] = decision.get("reason")
            existing["last_report_json"] = deepcopy(decision.get("report", {}))
            existing["ticket_status"] = decision.get("ticket_status", existing.get("ticket_status", "not_applicable"))
            existing["ticket_no"] = decision.get("ticket_no", existing.get("ticket_no"))
            existing["transfer_to_ticket"] = bool(decision.get("transfer_to_ticket", existing.get("transfer_to_ticket")))
            return existing

        incident_no = f"INC{datetime.now(UTC).strftime('%Y%m%d')}{len(self.incidents) + 1:04d}"
        incident = {
            "incident_no": incident_no,
            "status": "open",
            "priority": decision.get("priority"),
            "root_alert_event_id": alert_event_id,
            "root_node_key": root_key,
            "dedup_key": dedup_key,
            "summary": alert.get("alert_name"),
            "host_name": alert.get("host_name"),
            "service": alert.get("tags", {}).get("service"),
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "last_push_at": None,
            "push_count": 0,
            "transfer_to_ticket": bool(decision.get("transfer_to_ticket")),
            "ticket_no": decision.get("ticket_no"),
            "ticket_status": decision.get("ticket_status", "not_applicable"),
            "last_decision_type": decision.get("decision"),
            "last_reason": decision.get("reason"),
            "last_report_json": deepcopy(decision.get("report", {})),
            "created_at": now,
            "updated_at": now,
        }
        self.incidents[incident_no] = incident
        return incident

    def update_incident_delivery(self, incident_no: str, delivery: dict) -> None:
        incident = self.incidents.get(incident_no)
        if not incident:
            return
        incident["last_push_at"] = delivery.get("last_push_at", incident.get("last_push_at"))
        incident["push_count"] = delivery.get("push_count", incident.get("push_count", 0))
        incident["transfer_to_ticket"] = bool(delivery.get("transfer_to_ticket", incident.get("transfer_to_ticket")))
        incident["ticket_no"] = delivery.get("ticket_no", incident.get("ticket_no"))
        incident["ticket_status"] = delivery.get("ticket_status", incident.get("ticket_status", "not_applicable"))
        incident["updated_at"] = _utc_now_iso()

    def close_incidents_for_alert(self, alert: dict) -> list[str]:
        dedup_key = self.build_dedup_key(alert)
        closed = []
        for incident in self.incidents.values():
            if incident["status"] == "open" and incident.get("dedup_key") == dedup_key:
                incident["status"] = "resolved"
                incident["resolved_at"] = _alert_reference_time_iso(alert)
                incident["updated_at"] = _utc_now_iso()
                closed.append(incident["incident_no"])
        return closed

    def clear_runtime_data(self) -> dict:
        result = {
            "alert_events": len(self.alert_events),
            "alert_decisions": len(self.alert_decisions),
            "incidents": len(self.incidents),
            "incident_alert_rels": len(self.incident_alert_rels),
            "chat_sessions": len(self.chat_sessions),
            "chat_messages": len(self.chat_messages),
        }
        self.alert_events.clear()
        self.alert_decisions.clear()
        self.incidents.clear()
        self.incident_alert_rels.clear()
        self.chat_sessions.clear()
        self.chat_messages.clear()
        return result

    def link_alert_to_incident(self, incident_no: str, alert_event_id: int) -> None:
        self.incident_alert_rels.append(
            {
                "incident_no": incident_no,
                "alert_event_id": alert_event_id,
                "rel_type": "member",
                "created_at": _utc_now_iso(),
            }
        )

    def save_chat_session(self, session_id: str, metadata: dict | None = None) -> None:
        now = _utc_now_iso()
        existing = self.chat_sessions.get(session_id)
        if existing:
            existing["updated_at"] = now
            existing["last_message_at"] = now
            if metadata is not None:
                existing["metadata_json"] = deepcopy(metadata)
            return
        self.chat_sessions[session_id] = {
            "session_id": session_id,
            "status": "open",
            "metadata_json": deepcopy(metadata or {}),
            "created_at": now,
            "updated_at": now,
            "last_message_at": now,
        }

    def save_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        trace: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> int:
        self.save_chat_session(session_id)
        message_id = len(self.chat_messages) + 1
        now = _utc_now_iso()
        self.chat_messages.append(
            {
                "id": message_id,
                "session_id": session_id,
                "role": role,
                "content": content,
                "trace_json": deepcopy(trace or []),
                "metadata_json": deepcopy(metadata or {}),
                "created_at": now,
            }
        )
        if session_id in self.chat_sessions:
            self.chat_sessions[session_id]["updated_at"] = now
            self.chat_sessions[session_id]["last_message_at"] = now
        return message_id

    @staticmethod
    def _build_root_key(alert: dict) -> str:
        service = alert.get("tags", {}).get("service", "")
        return f"{alert.get('host_name', '')}:{service}:{alert.get('alert_name', '')}"

    @staticmethod
    def build_dedup_key(alert: dict) -> str:
        resource_scope = json.dumps(alert.get("resource_scope", {}), ensure_ascii=False, sort_keys=True)
        signal = json.dumps(alert.get("signal", {}), ensure_ascii=False, sort_keys=True)
        service = alert.get("tags", {}).get("service", "")
        host_identity = alert.get("host_name", "") or alert.get("host_ip", "")
        return "|".join(
            [
                alert.get("source", "zabbix"),
                host_identity,
                alert.get("alert_type", "generic"),
                service,
                alert.get("alert_name", ""),
                resource_scope,
                signal,
            ]
        )


class SQLStore:
    def __init__(self, database_url: str) -> None:
        from sqlalchemy import create_engine

        self.engine = create_engine(database_url, future=True, pool_pre_ping=True)

    def initialize(self) -> None:
        from sqlalchemy import text

        statements = self._build_create_statements()

        with self.engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
        self._ensure_columns()

    def _build_create_statements(self) -> list[str]:
        if self.engine.dialect.name == "sqlite":
            return self._build_sqlite_statements()
        return self._build_mysql_statements()

    @staticmethod
    def _build_sqlite_statements() -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS alert_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                tags_json TEXT,
                event_time VARCHAR(64),
                ingest_time VARCHAR(64),
                dedup_key VARCHAR(1024),
                raw_payload_json TEXT,
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS alert_decision (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_event_id INTEGER NOT NULL,
                incident_no VARCHAR(128),
                decision_type VARCHAR(32) NOT NULL,
                priority VARCHAR(32),
                need_push INTEGER NOT NULL DEFAULT 0,
                push_suppressed INTEGER NOT NULL DEFAULT 0,
                push_suppressed_reason TEXT,
                dedup_window_until VARCHAR(64),
                reason_summary TEXT,
                merge_target_incident_no VARCHAR(128),
                observe_until VARCHAR(64),
                action VARCHAR(32),
                transfer_to_ticket INTEGER NOT NULL DEFAULT 0,
                ticket_no VARCHAR(128),
                ticket_status VARCHAR(64),
                ticket_payload_json TEXT,
                ai_plan_json TEXT,
                context_json TEXT,
                model_decision_json TEXT,
                report_json TEXT,
                executed_at VARCHAR(64),
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_no VARCHAR(128) NOT NULL UNIQUE,
                status VARCHAR(32) NOT NULL,
                priority VARCHAR(32),
                root_alert_event_id INTEGER,
                root_node_key VARCHAR(255),
                dedup_key VARCHAR(1024),
                summary TEXT,
                host_name VARCHAR(255),
                service VARCHAR(255),
                first_seen_at VARCHAR(64),
                last_seen_at VARCHAR(64),
                last_push_at VARCHAR(64),
                push_count INTEGER NOT NULL DEFAULT 0,
                transfer_to_ticket INTEGER NOT NULL DEFAULT 0,
                ticket_no VARCHAR(128),
                ticket_status VARCHAR(64),
                last_decision_type VARCHAR(32),
                last_reason TEXT,
                last_report_json TEXT,
                resolved_at VARCHAR(64),
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident_alert_rel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_no VARCHAR(128) NOT NULL,
                alert_event_id INTEGER NOT NULL,
                rel_type VARCHAR(32) NOT NULL DEFAULT 'member',
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_session (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(128) NOT NULL UNIQUE,
                status VARCHAR(32) NOT NULL DEFAULT 'open',
                metadata_json TEXT,
                created_at VARCHAR(64),
                updated_at VARCHAR(64),
                last_message_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_message (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id VARCHAR(128) NOT NULL,
                role VARCHAR(32) NOT NULL,
                content TEXT,
                trace_json TEXT,
                metadata_json TEXT,
                created_at VARCHAR(64)
            )
            """,
        ]

    @staticmethod
    def _build_mysql_statements() -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS alert_event (
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
                tags_json TEXT,
                event_time VARCHAR(64),
                ingest_time VARCHAR(64),
                dedup_key VARCHAR(1024),
                raw_payload_json TEXT,
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS alert_decision (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                alert_event_id BIGINT NOT NULL,
                incident_no VARCHAR(128),
                decision_type VARCHAR(32) NOT NULL,
                priority VARCHAR(32),
                need_push TINYINT NOT NULL DEFAULT 0,
                push_suppressed TINYINT NOT NULL DEFAULT 0,
                push_suppressed_reason TEXT,
                dedup_window_until VARCHAR(64),
                reason_summary TEXT,
                merge_target_incident_no VARCHAR(128),
                observe_until VARCHAR(64),
                action VARCHAR(32),
                transfer_to_ticket TINYINT NOT NULL DEFAULT 0,
                ticket_no VARCHAR(128),
                ticket_status VARCHAR(64),
                ticket_payload_json TEXT,
                ai_plan_json TEXT,
                context_json TEXT,
                model_decision_json LONGTEXT,
                report_json LONGTEXT,
                executed_at VARCHAR(64),
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                incident_no VARCHAR(128) NOT NULL UNIQUE,
                status VARCHAR(32) NOT NULL,
                priority VARCHAR(32),
                root_alert_event_id BIGINT,
                root_node_key VARCHAR(255),
                dedup_key VARCHAR(1024),
                summary TEXT,
                host_name VARCHAR(255),
                service VARCHAR(255),
                first_seen_at VARCHAR(64),
                last_seen_at VARCHAR(64),
                last_push_at VARCHAR(64),
                push_count INT NOT NULL DEFAULT 0,
                transfer_to_ticket TINYINT NOT NULL DEFAULT 0,
                ticket_no VARCHAR(128),
                ticket_status VARCHAR(64),
                last_decision_type VARCHAR(32),
                last_reason TEXT,
                last_report_json LONGTEXT,
                resolved_at VARCHAR(64),
                created_at VARCHAR(64),
                updated_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident_alert_rel (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                incident_no VARCHAR(128) NOT NULL,
                alert_event_id BIGINT NOT NULL,
                rel_type VARCHAR(32) NOT NULL DEFAULT 'member',
                created_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_session (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                session_id VARCHAR(128) NOT NULL UNIQUE,
                status VARCHAR(32) NOT NULL DEFAULT 'open',
                metadata_json LONGTEXT,
                created_at VARCHAR(64),
                updated_at VARCHAR(64),
                last_message_at VARCHAR(64)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chat_message (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                session_id VARCHAR(128) NOT NULL,
                role VARCHAR(32) NOT NULL,
                content LONGTEXT,
                trace_json LONGTEXT,
                metadata_json LONGTEXT,
                created_at VARCHAR(64)
            )
            """,
        ]

    @property
    def backend_name(self) -> str:
        return "sql"

    def _ensure_columns(self) -> None:
        existing = {
            "alert_event": self._list_columns("alert_event"),
            "alert_decision": self._list_columns("alert_decision"),
            "incident": self._list_columns("incident"),
            "chat_session": self._list_columns("chat_session"),
            "chat_message": self._list_columns("chat_message"),
        }
        expected = {
            "alert_event": {
                "dedup_key": "VARCHAR(1024)",
            },
            "alert_decision": {
                "push_suppressed": "INTEGER NOT NULL DEFAULT 0",
                "push_suppressed_reason": "TEXT",
                "dedup_window_until": "VARCHAR(64)",
                "transfer_to_ticket": "INTEGER NOT NULL DEFAULT 0",
                "ticket_no": "VARCHAR(128)",
                "ticket_status": "VARCHAR(64)",
                "ticket_payload_json": "TEXT",
                "ai_plan_json": "TEXT",
                "context_json": "TEXT",
                "model_decision_json": "TEXT",
                "report_json": "TEXT",
            },
            "incident": {
                "dedup_key": "VARCHAR(1024)",
                "last_push_at": "VARCHAR(64)",
                "push_count": "INTEGER NOT NULL DEFAULT 0",
                "transfer_to_ticket": "INTEGER NOT NULL DEFAULT 0",
                "ticket_no": "VARCHAR(128)",
                "ticket_status": "VARCHAR(64)",
                "last_decision_type": "VARCHAR(32)",
                "last_reason": "TEXT",
                "last_report_json": "TEXT",
                "resolved_at": "VARCHAR(64)",
            },
            "chat_session": {
                "status": "VARCHAR(32) NOT NULL DEFAULT 'open'",
                "metadata_json": "TEXT",
                "updated_at": "VARCHAR(64)",
                "last_message_at": "VARCHAR(64)",
            },
            "chat_message": {
                "trace_json": "TEXT",
                "metadata_json": "TEXT",
            },
        }

        from sqlalchemy import text

        with self.engine.begin() as conn:
            for table_name, columns in expected.items():
                for column_name, ddl in columns.items():
                    if column_name in existing.get(table_name, set()):
                        continue
                    try:
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))
                    except Exception as exc:  # pragma: no cover
                        logger.warning("Failed to add column %s.%s: %s", table_name, column_name, exc)

    def _list_columns(self, table_name: str) -> set[str]:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            if self.engine.dialect.name == "sqlite":
                rows = conn.execute(text(f"PRAGMA table_info({table_name})")).mappings()
                return {row["name"] for row in rows}
            try:
                rows = conn.execute(text(f"SHOW COLUMNS FROM {table_name}")).mappings()
                return {row["Field"] for row in rows}
            except Exception:  # pragma: no cover
                return set()

    def healthcheck(self) -> dict:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text("SELECT 1"))
            alert_events = conn.execute(text("SELECT COUNT(1) FROM alert_event")).scalar_one()
            incidents = conn.execute(text("SELECT COUNT(1) FROM incident")).scalar_one()
            chat_sessions = conn.execute(text("SELECT COUNT(1) FROM chat_session")).scalar_one()
            chat_messages = conn.execute(text("SELECT COUNT(1) FROM chat_message")).scalar_one()
        return {
            "backend": self.backend_name,
            "healthy": True,
            "details": {
                "mode": "database",
                "alert_events": int(alert_events),
                "incidents": int(incidents),
                "chat_sessions": int(chat_sessions),
                "chat_messages": int(chat_messages),
            },
        }

    def save_alert_event(self, alert: dict, raw_payload: dict) -> int:
        from sqlalchemy import text

        now = _utc_now_iso()
        payload = {
            "source": alert.get("source", "zabbix"),
            "source_event_id": alert.get("source_event_id"),
            "source_problem_id": alert.get("source_problem_id"),
            "trigger_id": alert.get("trigger_id"),
            "host_id": alert.get("host_id"),
            "host_name": alert.get("host_name"),
            "host_ip": alert.get("host_ip"),
            "severity": alert.get("severity"),
            "status": alert.get("status"),
            "alert_name": alert.get("alert_name"),
            "alert_message": alert.get("alert_message"),
            "tags_json": json.dumps(alert.get("tags", {}), ensure_ascii=False),
            "event_time": alert.get("event_time"),
            "ingest_time": now,
            "dedup_key": self.build_dedup_key(alert),
            "raw_payload_json": json.dumps(raw_payload, ensure_ascii=False),
            "created_at": now,
        }
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO alert_event (
                        source, source_event_id, source_problem_id, trigger_id, host_id,
                        host_name, host_ip, severity, status, alert_name, alert_message,
                        tags_json, event_time, ingest_time, dedup_key, raw_payload_json, created_at
                    ) VALUES (
                        :source, :source_event_id, :source_problem_id, :trigger_id, :host_id,
                        :host_name, :host_ip, :severity, :status, :alert_name, :alert_message,
                        :tags_json, :event_time, :ingest_time, :dedup_key, :raw_payload_json, :created_at
                    )
                    """
                ),
                payload,
            )
            return int(result.lastrowid)

    def save_alert_decision(self, alert_event_id: int, decision: dict, plan: dict | None = None, context: dict | None = None) -> int:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO alert_decision (
                        alert_event_id, incident_no, decision_type, priority, need_push,
                        push_suppressed, push_suppressed_reason, dedup_window_until,
                        reason_summary, merge_target_incident_no, observe_until, action,
                        transfer_to_ticket, ticket_no, ticket_status, ticket_payload_json,
                        ai_plan_json, context_json, model_decision_json, report_json,
                        executed_at, created_at
                    ) VALUES (
                        :alert_event_id, :incident_no, :decision_type, :priority, :need_push,
                        :push_suppressed, :push_suppressed_reason, :dedup_window_until,
                        :reason_summary, :merge_target_incident_no, :observe_until, :action,
                        :transfer_to_ticket, :ticket_no, :ticket_status, :ticket_payload_json,
                        :ai_plan_json, :context_json, :model_decision_json, :report_json,
                        :executed_at, :created_at
                    )
                    """
                ),
                {
                    "alert_event_id": alert_event_id,
                    "incident_no": decision.get("incident_no"),
                    "decision_type": decision.get("decision", "observe"),
                    "priority": decision.get("priority"),
                    "need_push": 1 if decision.get("need_push") else 0,
                    "push_suppressed": 1 if decision.get("push_suppressed") else 0,
                    "push_suppressed_reason": decision.get("push_suppressed_reason"),
                    "dedup_window_until": decision.get("dedup_window_until"),
                    "reason_summary": decision.get("reason"),
                    "merge_target_incident_no": decision.get("merge_target_incident_no"),
                    "observe_until": decision.get("observe_until"),
                    "action": decision.get("action"),
                    "transfer_to_ticket": 1 if decision.get("transfer_to_ticket") else 0,
                    "ticket_no": decision.get("ticket_no"),
                    "ticket_status": decision.get("ticket_status"),
                    "ticket_payload_json": json.dumps(decision.get("ticket_payload", {}), ensure_ascii=False),
                    "ai_plan_json": json.dumps(plan or {}, ensure_ascii=False),
                    "context_json": json.dumps(context or {}, ensure_ascii=False),
                    "model_decision_json": json.dumps(decision, ensure_ascii=False),
                    "report_json": json.dumps(decision.get("report", {}), ensure_ascii=False),
                    "executed_at": decision.get("executed_at"),
                    "created_at": _utc_now_iso(),
                },
            )
            return int(result.lastrowid)

    def get_related_incidents(self, alert: dict) -> list[dict]:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT incident_no, status, priority, ticket_status
                    FROM incident
                    WHERE status = 'open' AND (dedup_key = :dedup_key OR host_name = :host_name OR service = :service)
                    ORDER BY created_at ASC
                    """
                ),
                {
                    "dedup_key": self.build_dedup_key(alert),
                    "host_name": alert.get("host_name"),
                    "service": alert.get("tags", {}).get("service"),
                },
            ).mappings()
            return [dict(row) for row in rows]

    def incident_exists(self, incident_no: str) -> bool:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            result = conn.execute(
                text("SELECT 1 FROM incident WHERE incident_no = :incident_no LIMIT 1"),
                {"incident_no": incident_no},
            ).first()
            return result is not None

    def get_incident(self, incident_no: str) -> dict | None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM incident WHERE incident_no = :incident_no LIMIT 1"),
                {"incident_no": incident_no},
            ).mappings().first()
            return dict(row) if row else None

    def get_open_incident_by_alert(self, alert: dict) -> dict | None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT *
                    FROM incident
                    WHERE status = 'open' AND dedup_key = :dedup_key
                    LIMIT 1
                    """
                ),
                {"dedup_key": self.build_dedup_key(alert)},
            ).mappings().first()
            return dict(row) if row else None

    def touch_incident(self, incident_no: str, seen_at: str | None = None) -> None:
        from sqlalchemy import text

        now = _utc_now_iso()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE incident
                    SET last_seen_at = :last_seen_at, updated_at = :updated_at
                    WHERE incident_no = :incident_no
                    """
                ),
                {
                    "incident_no": incident_no,
                    "last_seen_at": seen_at or now,
                    "updated_at": now,
                },
            )

    def create_or_update_incident(self, alert_event_id: int, alert: dict, decision: dict) -> dict:
        from sqlalchemy import text

        root_key = InMemoryStore._build_root_key(alert)
        dedup_key = self.build_dedup_key(alert)
        now = _utc_now_iso()
        seen_at = _alert_reference_time_iso(alert)

        with self.engine.begin() as conn:
            existing = conn.execute(
                text(
                    """
                    SELECT *
                    FROM incident
                    WHERE status = 'open' AND dedup_key = :dedup_key
                    LIMIT 1
                    """
                ),
                {"dedup_key": dedup_key},
            ).mappings().first()

            if existing:
                conn.execute(
                    text(
                        """
                        UPDATE incident
                        SET priority = :priority,
                            summary = :summary,
                            last_seen_at = :last_seen_at,
                            updated_at = :updated_at,
                            last_decision_type = :last_decision_type,
                            last_reason = :last_reason,
                            last_report_json = :last_report_json,
                            transfer_to_ticket = :transfer_to_ticket,
                            ticket_no = :ticket_no,
                            ticket_status = :ticket_status
                        WHERE incident_no = :incident_no
                        """
                    ),
                    {
                        "incident_no": existing["incident_no"],
                        "priority": decision.get("priority"),
                        "summary": alert.get("alert_name"),
                        "last_seen_at": seen_at,
                        "updated_at": now,
                        "last_decision_type": decision.get("decision"),
                        "last_reason": decision.get("reason"),
                        "last_report_json": json.dumps(decision.get("report", {}), ensure_ascii=False),
                        "transfer_to_ticket": 1 if decision.get("transfer_to_ticket") else int(existing.get("transfer_to_ticket") or 0),
                        "ticket_no": decision.get("ticket_no") or existing.get("ticket_no"),
                        "ticket_status": decision.get("ticket_status") or existing.get("ticket_status") or "not_applicable",
                    },
                )
                updated = dict(existing)
                updated["priority"] = decision.get("priority")
                updated["summary"] = alert.get("alert_name")
                updated["last_seen_at"] = seen_at
                updated["updated_at"] = now
                updated["last_decision_type"] = decision.get("decision")
                updated["last_reason"] = decision.get("reason")
                updated["last_report_json"] = json.dumps(decision.get("report", {}), ensure_ascii=False)
                updated["transfer_to_ticket"] = 1 if decision.get("transfer_to_ticket") else int(existing.get("transfer_to_ticket") or 0)
                updated["ticket_no"] = decision.get("ticket_no") or existing.get("ticket_no")
                updated["ticket_status"] = decision.get("ticket_status") or existing.get("ticket_status") or "not_applicable"
                return updated

            incident_no = self._generate_incident_no(conn)
            incident = {
                "incident_no": incident_no,
                "status": "open",
                "priority": decision.get("priority"),
                "root_alert_event_id": alert_event_id,
                "root_node_key": root_key,
                "dedup_key": dedup_key,
                "summary": alert.get("alert_name"),
                "host_name": alert.get("host_name"),
                "service": alert.get("tags", {}).get("service"),
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "last_push_at": None,
                "push_count": 0,
                "transfer_to_ticket": 1 if decision.get("transfer_to_ticket") else 0,
                "ticket_no": decision.get("ticket_no"),
                "ticket_status": decision.get("ticket_status", "not_applicable"),
                "last_decision_type": decision.get("decision"),
                "last_reason": decision.get("reason"),
                "last_report_json": json.dumps(decision.get("report", {}), ensure_ascii=False),
                "resolved_at": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                text(
                    """
                    INSERT INTO incident (
                        incident_no, status, priority, root_alert_event_id, root_node_key, dedup_key, summary,
                        host_name, service, first_seen_at, last_seen_at, last_push_at, push_count,
                        transfer_to_ticket, ticket_no, ticket_status, last_decision_type, last_reason,
                        last_report_json, resolved_at, created_at, updated_at
                    ) VALUES (
                        :incident_no, :status, :priority, :root_alert_event_id, :root_node_key, :dedup_key, :summary,
                        :host_name, :service, :first_seen_at, :last_seen_at, :last_push_at, :push_count,
                        :transfer_to_ticket, :ticket_no, :ticket_status, :last_decision_type, :last_reason,
                        :last_report_json, :resolved_at, :created_at, :updated_at
                    )
                    """
                ),
                incident,
            )
            return incident

    def update_incident_delivery(self, incident_no: str, delivery: dict) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE incident
                    SET last_push_at = :last_push_at,
                        push_count = :push_count,
                        transfer_to_ticket = :transfer_to_ticket,
                        ticket_no = :ticket_no,
                        ticket_status = :ticket_status,
                        updated_at = :updated_at
                    WHERE incident_no = :incident_no
                    """
                ),
                {
                    "incident_no": incident_no,
                    "last_push_at": delivery.get("last_push_at"),
                    "push_count": delivery.get("push_count", 0),
                    "transfer_to_ticket": 1 if delivery.get("transfer_to_ticket") else 0,
                    "ticket_no": delivery.get("ticket_no"),
                    "ticket_status": delivery.get("ticket_status"),
                    "updated_at": _utc_now_iso(),
                },
            )

    def close_incidents_for_alert(self, alert: dict) -> list[str]:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT incident_no
                    FROM incident
                    WHERE status = 'open' AND dedup_key = :dedup_key
                    """
                ),
                {"dedup_key": self.build_dedup_key(alert)},
            ).mappings().all()
            incident_nos = [row["incident_no"] for row in rows]
            if incident_nos:
                conn.execute(
                    text(
                        """
                        UPDATE incident
                        SET status = 'resolved', resolved_at = :resolved_at, updated_at = :updated_at
                        WHERE status = 'open' AND dedup_key = :dedup_key
                        """
                    ),
                    {
                        "dedup_key": self.build_dedup_key(alert),
                        "resolved_at": _alert_reference_time_iso(alert),
                        "updated_at": _utc_now_iso(),
                    },
                )
            return incident_nos

    def link_alert_to_incident(self, incident_no: str, alert_event_id: int) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO incident_alert_rel (incident_no, alert_event_id, rel_type, created_at)
                    VALUES (:incident_no, :alert_event_id, :rel_type, :created_at)
                    """
                ),
                {
                    "incident_no": incident_no,
                    "alert_event_id": alert_event_id,
                    "rel_type": "member",
                    "created_at": _utc_now_iso(),
                },
            )

    def clear_runtime_data(self) -> dict:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            result = {
                "alert_events": int(conn.execute(text("SELECT COUNT(1) FROM alert_event")).scalar_one()),
                "alert_decisions": int(conn.execute(text("SELECT COUNT(1) FROM alert_decision")).scalar_one()),
                "incidents": int(conn.execute(text("SELECT COUNT(1) FROM incident")).scalar_one()),
                "incident_alert_rels": int(conn.execute(text("SELECT COUNT(1) FROM incident_alert_rel")).scalar_one()),
                "chat_sessions": int(conn.execute(text("SELECT COUNT(1) FROM chat_session")).scalar_one()),
                "chat_messages": int(conn.execute(text("SELECT COUNT(1) FROM chat_message")).scalar_one()),
            }
            conn.execute(text("DELETE FROM chat_message"))
            conn.execute(text("DELETE FROM chat_session"))
            conn.execute(text("DELETE FROM incident_alert_rel"))
            conn.execute(text("DELETE FROM alert_decision"))
            conn.execute(text("DELETE FROM incident"))
            conn.execute(text("DELETE FROM alert_event"))
        return result

    def save_chat_session(self, session_id: str, metadata: dict | None = None) -> None:
        from sqlalchemy import text

        now = _utc_now_iso()
        payload = {
            "session_id": session_id,
            "status": "open",
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
            "last_message_at": now,
        }
        with self.engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM chat_session WHERE session_id = :session_id LIMIT 1"),
                {"session_id": session_id},
            ).first()
            if exists:
                conn.execute(
                    text(
                        """
                        UPDATE chat_session
                        SET metadata_json = :metadata_json,
                            updated_at = :updated_at,
                            last_message_at = :last_message_at
                        WHERE session_id = :session_id
                        """
                    ),
                    payload,
                )
            else:
                conn.execute(
                    text(
                        """
                        INSERT INTO chat_session (
                            session_id, status, metadata_json, created_at, updated_at, last_message_at
                        ) VALUES (
                            :session_id, :status, :metadata_json, :created_at, :updated_at, :last_message_at
                        )
                        """
                    ),
                    payload,
                )

    def save_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        trace: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> int:
        from sqlalchemy import text

        self.save_chat_session(session_id)
        now = _utc_now_iso()
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO chat_message (
                        session_id, role, content, trace_json, metadata_json, created_at
                    ) VALUES (
                        :session_id, :role, :content, :trace_json, :metadata_json, :created_at
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "role": role,
                    "content": content,
                    "trace_json": json.dumps(trace or [], ensure_ascii=False),
                    "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
                    "created_at": now,
                },
            )
            conn.execute(
                text(
                    """
                    UPDATE chat_session
                    SET updated_at = :updated_at, last_message_at = :last_message_at
                    WHERE session_id = :session_id
                    """
                ),
                {
                    "session_id": session_id,
                    "updated_at": now,
                    "last_message_at": now,
                },
            )
            return int(result.lastrowid)

    @staticmethod
    def _generate_incident_no(conn: Any) -> str:
        from sqlalchemy import text

        prefix = datetime.now(UTC).strftime("INC%Y%m%d")
        count = conn.execute(
            text("SELECT COUNT(1) FROM incident WHERE incident_no LIKE :prefix"),
            {"prefix": f"{prefix}%"},
        ).scalar_one()
        return f"{prefix}{int(count) + 1:04d}"

    @staticmethod
    def build_dedup_key(alert: dict) -> str:
        return InMemoryStore.build_dedup_key(alert)


def create_store(config: Any) -> InMemoryStore | SQLStore:
    backend = str(getattr(config, "STORE_BACKEND", "memory")).lower()
    database_url = getattr(config, "DATABASE_URL", "")

    if backend in {"sql", "tidb"} and database_url:
        try:
            store = SQLStore(database_url)
            store.initialize()
            logger.info("Using SQL store backend.")
            return store
        except Exception as exc:  # pragma: no cover
            logger.warning("Falling back to in-memory store because SQL store init failed: %s", exc)

    logger.info("Using in-memory store backend.")
    return InMemoryStore()


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
