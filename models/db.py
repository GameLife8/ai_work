from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


logger = logging.getLogger(__name__)


class InMemoryStore:
    def __init__(self) -> None:
        self.alert_events: list[dict] = []
        self.alert_decisions: list[dict] = []
        self.incidents: dict[str, dict] = {}
        self.incident_alert_rels: list[dict] = []

    def save_alert_event(self, alert: dict, raw_payload: dict) -> int:
        alert_event_id = len(self.alert_events) + 1
        record = deepcopy(alert)
        record["id"] = alert_event_id
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
            },
        }

    def save_alert_decision(self, alert_event_id: int, decision: dict) -> int:
        decision_id = len(self.alert_decisions) + 1
        record = deepcopy(decision)
        record["id"] = decision_id
        record["alert_event_id"] = alert_event_id
        record["created_at"] = _utc_now_iso()
        self.alert_decisions.append(record)
        return decision_id

    def get_related_incidents(self, alert: dict) -> list[dict]:
        host_name = alert.get("host_name")
        service = alert.get("tags", {}).get("service")
        related = []
        for incident in self.incidents.values():
            if incident["status"] != "open":
                continue
            if incident.get("host_name") == host_name or incident.get("service") == service:
                related.append(
                    {
                        "incident_no": incident["incident_no"],
                        "status": incident["status"],
                        "priority": incident["priority"],
                    }
                )
        return related

    def incident_exists(self, incident_no: str) -> bool:
        return incident_no in self.incidents

    def touch_incident(self, incident_no: str) -> None:
        if incident_no in self.incidents:
            self.incidents[incident_no]["last_seen_at"] = _utc_now_iso()
            self.incidents[incident_no]["updated_at"] = _utc_now_iso()

    def create_or_update_incident(self, alert_event_id: int, alert: dict, decision: dict) -> dict:
        root_key = self._build_root_key(alert)
        existing = next((item for item in self.incidents.values() if item["root_node_key"] == root_key), None)
        now = _utc_now_iso()

        if existing:
            existing["last_seen_at"] = now
            existing["updated_at"] = now
            existing["priority"] = decision.get("priority")
            return existing

        incident_no = f"INC{datetime.now(UTC).strftime('%Y%m%d')}{len(self.incidents) + 1:04d}"
        incident = {
            "incident_no": incident_no,
            "status": "open",
            "priority": decision.get("priority"),
            "root_alert_event_id": alert_event_id,
            "root_node_key": root_key,
            "summary": alert.get("alert_name"),
            "host_name": alert.get("host_name"),
            "service": alert.get("tags", {}).get("service"),
            "first_seen_at": now,
            "last_seen_at": now,
            "created_at": now,
            "updated_at": now,
        }
        self.incidents[incident_no] = incident
        return incident

    def link_alert_to_incident(self, incident_no: str, alert_event_id: int) -> None:
        self.incident_alert_rels.append(
            {
                "incident_no": incident_no,
                "alert_event_id": alert_event_id,
                "rel_type": "member",
                "created_at": _utc_now_iso(),
            }
        )

    @staticmethod
    def _build_root_key(alert: dict) -> str:
        service = alert.get("tags", {}).get("service", "")
        return f"{alert.get('host_name', '')}:{service}:{alert.get('alert_name', '')}"


class SQLStore:
    def __init__(self, database_url: str) -> None:
        from sqlalchemy import create_engine

        self.engine = create_engine(database_url, future=True, pool_pre_ping=True)

    def initialize(self) -> None:
        from sqlalchemy import text

        statements = [
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
                reason_summary TEXT,
                merge_target_incident_no VARCHAR(128),
                observe_until VARCHAR(64),
                action VARCHAR(32),
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
                summary TEXT,
                host_name VARCHAR(255),
                service VARCHAR(255),
                first_seen_at VARCHAR(64),
                last_seen_at VARCHAR(64),
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
        ]

        with self.engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))

    @property
    def backend_name(self) -> str:
        return "sql"

    def healthcheck(self) -> dict:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(text("SELECT 1"))
            alert_events = conn.execute(text("SELECT COUNT(1) FROM alert_event")).scalar_one()
            incidents = conn.execute(text("SELECT COUNT(1) FROM incident")).scalar_one()
        return {
            "backend": self.backend_name,
            "healthy": True,
            "details": {
                "mode": "database",
                "alert_events": int(alert_events),
                "incidents": int(incidents),
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
                        tags_json, event_time, ingest_time, raw_payload_json, created_at
                    ) VALUES (
                        :source, :source_event_id, :source_problem_id, :trigger_id, :host_id,
                        :host_name, :host_ip, :severity, :status, :alert_name, :alert_message,
                        :tags_json, :event_time, :ingest_time, :raw_payload_json, :created_at
                    )
                    """
                ),
                payload,
            )
            return int(result.lastrowid)

    def save_alert_decision(self, alert_event_id: int, decision: dict) -> int:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    INSERT INTO alert_decision (
                        alert_event_id, incident_no, decision_type, priority, need_push,
                        reason_summary, merge_target_incident_no, observe_until, action,
                        executed_at, created_at
                    ) VALUES (
                        :alert_event_id, :incident_no, :decision_type, :priority, :need_push,
                        :reason_summary, :merge_target_incident_no, :observe_until, :action,
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
                    "reason_summary": decision.get("reason"),
                    "merge_target_incident_no": decision.get("merge_target_incident_no"),
                    "observe_until": decision.get("observe_until"),
                    "action": decision.get("action"),
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
                    SELECT incident_no, status, priority
                    FROM incident
                    WHERE status = 'open' AND (host_name = :host_name OR service = :service)
                    ORDER BY created_at ASC
                    """
                ),
                {
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

    def touch_incident(self, incident_no: str) -> None:
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
                    "last_seen_at": now,
                    "updated_at": now,
                },
            )

    def create_or_update_incident(self, alert_event_id: int, alert: dict, decision: dict) -> dict:
        from sqlalchemy import text

        root_key = InMemoryStore._build_root_key(alert)
        now = _utc_now_iso()

        with self.engine.begin() as conn:
            existing = conn.execute(
                text(
                    """
                    SELECT incident_no, status, priority, root_node_key, summary, host_name, service,
                           first_seen_at, last_seen_at, created_at, updated_at
                    FROM incident
                    WHERE root_node_key = :root_node_key
                    LIMIT 1
                    """
                ),
                {"root_node_key": root_key},
            ).mappings().first()

            if existing:
                conn.execute(
                    text(
                        """
                        UPDATE incident
                        SET priority = :priority, last_seen_at = :last_seen_at, updated_at = :updated_at
                        WHERE incident_no = :incident_no
                        """
                    ),
                    {
                        "incident_no": existing["incident_no"],
                        "priority": decision.get("priority"),
                        "last_seen_at": now,
                        "updated_at": now,
                    },
                )
                updated = dict(existing)
                updated["priority"] = decision.get("priority")
                updated["last_seen_at"] = now
                updated["updated_at"] = now
                return updated

            incident_no = self._generate_incident_no(conn)
            incident = {
                "incident_no": incident_no,
                "status": "open",
                "priority": decision.get("priority"),
                "root_alert_event_id": alert_event_id,
                "root_node_key": root_key,
                "summary": alert.get("alert_name"),
                "host_name": alert.get("host_name"),
                "service": alert.get("tags", {}).get("service"),
                "first_seen_at": now,
                "last_seen_at": now,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                text(
                    """
                    INSERT INTO incident (
                        incident_no, status, priority, root_alert_event_id, root_node_key, summary,
                        host_name, service, first_seen_at, last_seen_at, created_at, updated_at
                    ) VALUES (
                        :incident_no, :status, :priority, :root_alert_event_id, :root_node_key, :summary,
                        :host_name, :service, :first_seen_at, :last_seen_at, :created_at, :updated_at
                    )
                    """
                ),
                incident,
            )
            return incident

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

    @staticmethod
    def _generate_incident_no(conn: Any) -> str:
        from sqlalchemy import text

        prefix = datetime.now(UTC).strftime("INC%Y%m%d")
        count = conn.execute(
            text("SELECT COUNT(1) FROM incident WHERE incident_no LIKE :prefix"),
            {"prefix": f"{prefix}%"},
        ).scalar_one()
        return f"{prefix}{int(count) + 1:04d}"


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
