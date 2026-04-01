from __future__ import annotations


class GraphClient:
    def resolve_topology(self, alert: dict) -> dict:
        service = alert.get("tags", {}).get("service")
        if service:
            return {"services": [service], "cluster": "default-cluster"}
        return {"services": [], "cluster": None}
