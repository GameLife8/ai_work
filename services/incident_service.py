from __future__ import annotations

class IncidentService:
    def __init__(self, store) -> None:
        self.store = store

    def get_related_incidents(self, alert: dict) -> list[dict]:
        return self.store.get_related_incidents(alert)

    def apply_decision(self, alert_event_id: int, alert: dict, decision: dict) -> str | None:
        decision_type = decision.get("decision")

        if decision_type == "merge":
            incident_no = decision.get("merge_target_incident_no")
            if incident_no and self.store.incident_exists(incident_no):
                self.store.link_alert_to_incident(incident_no, alert_event_id)
                self.store.touch_incident(incident_no)
                return incident_no
            return None

        if decision_type in {"notify", "observe"}:
            incident = self.store.create_or_update_incident(alert_event_id, alert, decision)
            self.store.link_alert_to_incident(incident["incident_no"], alert_event_id)
            return incident["incident_no"]

        return None
