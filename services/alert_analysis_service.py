from __future__ import annotations

from services.alert_parser import AlertParser


class AlertAnalysisService:
    def __init__(self, ai_client, context_fetcher, decision_engine, default_needs: list[str]) -> None:
        self.ai_client = ai_client
        self.context_fetcher = context_fetcher
        self.decision_engine = decision_engine
        self.default_needs = default_needs

    def analyze(self, raw_payload: dict) -> dict:
        alert = AlertParser.parse(raw_payload)
        plan = self.ai_client.plan_context(alert)
        needs = plan.get("needs") or self.default_needs
        context = self.context_fetcher.fetch_context(needs, alert)
        decision = self.ai_client.judge_alert(alert, context)
        decision["ai_plan"] = plan
        action_preview = self.decision_engine.execute_action(alert, decision)
        return {
            "alert": alert,
            "plan": plan,
            "context": context,
            "decision": {**decision, **action_preview},
        }
