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

        if "topology" in needs:
            context["topology"] = self.graph_client.resolve_topology(alert)

        if "related_incidents" in needs:
            context["related_incidents"] = self.incident_service.get_related_incidents(alert)

        return context
