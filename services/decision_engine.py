from __future__ import annotations

from datetime import UTC, datetime, timedelta


class DecisionEngine:
    def execute_action(self, alert: dict, decision: dict) -> dict:
        decision_type = decision.get("decision", "observe")
        now = datetime.now(UTC)
        result = {"action": decision_type, "executed_at": now.isoformat()}

        if decision_type == "observe":
            result["observe_until"] = (now + timedelta(minutes=30)).isoformat()
        if decision_type == "notify":
            result["need_push"] = True
        return result
