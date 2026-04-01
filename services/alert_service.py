from __future__ import annotations

from services.alert_parser import AlertParser


class AlertService:
    def __init__(
        self,
        store,
        ai_client,
        context_fetcher,
        incident_service,
        decision_engine,
        default_needs: list[str],
    ) -> None:
        self.store = store
        self.ai_client = ai_client
        self.context_fetcher = context_fetcher
        self.incident_service = incident_service
        self.decision_engine = decision_engine
        self.default_needs = default_needs

    def handle_alert(self, raw_payload: dict) -> dict:
        alert = AlertParser.parse(raw_payload)
        alert_event_id = self.store.save_alert_event(alert, raw_payload)

        plan = self.ai_client.plan_context(alert)
        needs = plan.get("needs") or self.default_needs

        context = self.context_fetcher.fetch_context(needs, alert)
        decision = self.ai_client.judge_alert(alert, context)

        action_result = self.decision_engine.execute_action(alert, decision)
        merged_decision = {**decision, **action_result}

        incident_no = self.incident_service.apply_decision(alert_event_id, alert, merged_decision)
        if incident_no:
            merged_decision["incident_no"] = incident_no

        self.store.save_alert_decision(alert_event_id, merged_decision)

        return {
            "alert_event_id": alert_event_id,
            "decision": merged_decision,
            "context": context,
        }
