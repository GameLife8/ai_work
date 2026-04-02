from __future__ import annotations

from datetime import UTC, datetime, timedelta


class IncidentService:
    DEDUP_WINDOW = timedelta(hours=24)

    def __init__(self, store) -> None:
        self.store = store

    def get_related_incidents(self, alert: dict) -> list[dict]:
        return self.store.get_related_incidents(alert)

    def apply_decision(self, alert_event_id: int, alert: dict, decision: dict) -> dict:
        if alert.get("status") == "resolved":
            closed_incidents = self.store.close_incidents_for_alert(alert)
            return {
                "incident_no": closed_incidents[0] if closed_incidents else None,
                "closed_incidents": closed_incidents,
                "need_push": False,
                "push_suppressed": True,
                "push_suppressed_reason": "alert_resolved",
                "transfer_to_ticket": False,
                "ticket_status": "resolved",
            }

        decision_type = decision.get("decision")

        if decision_type == "merge":
            incident_no = decision.get("merge_target_incident_no")
            if incident_no and self.store.incident_exists(incident_no):
                self.store.link_alert_to_incident(incident_no, alert_event_id)
                self.store.touch_incident(incident_no, seen_at=self._reference_time(alert).isoformat())
                delivery = self._build_delivery_result(alert, decision, self.store.get_incident(incident_no))
                self.store.update_incident_delivery(incident_no, delivery)
                return {"incident_no": incident_no, **delivery}
            return {"incident_no": None, "need_push": False}

        if decision_type in {"notify", "observe"}:
            incident = self.store.create_or_update_incident(alert_event_id, alert, decision)
            self.store.link_alert_to_incident(incident["incident_no"], alert_event_id)
            delivery = self._build_delivery_result(alert, decision, incident)
            self.store.update_incident_delivery(incident["incident_no"], delivery)
            return {"incident_no": incident["incident_no"], **delivery}

        return {"incident_no": None, "need_push": False}

    def _build_delivery_result(self, alert: dict, decision: dict, incident: dict | None) -> dict:
        incident = incident or {}
        ref_time = self._reference_time(alert)
        candidate_push = bool(decision.get("need_push")) and decision.get("decision") in {"notify", "merge"}
        last_push_at = self._parse_optional_time(incident.get("last_push_at"))
        should_push = candidate_push and (last_push_at is None or ref_time - last_push_at >= self.DEDUP_WINDOW)
        dedup_window_until = (ref_time + self.DEDUP_WINDOW).isoformat() if should_push else (
            (last_push_at + self.DEDUP_WINDOW).isoformat() if last_push_at else None
        )
        push_count = int(incident.get("push_count") or 0) + (1 if should_push else 0)
        transfer_to_ticket = bool(decision.get("transfer_to_ticket"))
        ticket_status = decision.get("ticket_status") or ("pending" if transfer_to_ticket else incident.get("ticket_status") or "not_applicable")

        return {
            "need_push": should_push,
            "push_suppressed": candidate_push and not should_push,
            "push_suppressed_reason": None if should_push or not candidate_push else "duplicate_within_24h",
            "dedup_window_until": dedup_window_until,
            "last_push_at": ref_time.isoformat() if should_push else incident.get("last_push_at"),
            "push_count": push_count if incident else (1 if should_push else 0),
            "transfer_to_ticket": transfer_to_ticket,
            "ticket_no": decision.get("ticket_no") or incident.get("ticket_no"),
            "ticket_status": ticket_status,
        }

    @staticmethod
    def _reference_time(alert: dict) -> datetime:
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

    @staticmethod
    def _parse_optional_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            return None
