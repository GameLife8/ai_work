from __future__ import annotations

from datetime import UTC, datetime, timedelta


class DecisionEngine:
    DEDUP_WINDOW_HOURS = 24

    def execute_action(self, alert: dict, decision: dict) -> dict:
        decision_type = decision.get("decision", "observe")
        now = datetime.now(UTC)
        priority = decision.get("priority", "P3")
        transfer_to_ticket = bool(decision.get("transfer_to_ticket", decision_type in {"notify", "merge"} and priority in {"P1", "P2"}))
        result = {
            "action": decision_type,
            "executed_at": now.isoformat(),
            "transfer_to_ticket": transfer_to_ticket,
            "ticket_status": decision.get("ticket_status") or ("pending" if transfer_to_ticket else "not_applicable"),
        }

        if decision_type == "observe":
            result["observe_until"] = (now + timedelta(minutes=30)).isoformat()
        if decision_type in {"notify", "merge"}:
            result["need_push"] = True
        return result
