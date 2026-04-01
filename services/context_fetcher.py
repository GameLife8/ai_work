from __future__ import annotations


class ContextFetcher:
    def __init__(self, zabbix_client, graph_client, incident_service) -> None:
        self.zabbix_client = zabbix_client
        self.graph_client = graph_client
        self.incident_service = incident_service

    def fetch_context(self, needs: list[str], alert: dict) -> dict:
        context = {}

        if "metric_summary" in needs:
            context["metric_summary"] = self.zabbix_client.get_metric_summary(alert)

        if "disk_summary" in needs:
            context["disk_summary"] = self.zabbix_client.get_disk_summary(alert)

        if "topology" in needs:
            context["topology"] = self.graph_client.resolve_topology(alert)

        if "related_incidents" in needs:
            context["related_incidents"] = self.incident_service.get_related_incidents(alert)

        if "alert_history" in needs:
            context["alert_history"] = self._build_alert_history(alert)

        return context

    @staticmethod
    def _build_alert_history(alert: dict) -> dict:
        return {
            "status": alert.get("status"),
            "event_time": alert.get("event_time"),
            "resolved": alert.get("status") == "resolved",
        }
