from __future__ import annotations

from services.context_fetcher import ContextFetcher


class _FakeZabbixClient:
    def get_metric_summary(self, alert):
        return {"cpu_avg": 1}

    def get_disk_summary(self, alert):
        return {"used_percent": 91}

    def get_memory_summary(self, alert):
        return {"memory_used_percent": 92}

    def get_disk_io_summary(self, alert):
        return {"utilization_percent": 93}

    def get_availability_summary(self, alert):
        return {"ping_status": "down"}


class _FakeGraphClient:
    def resolve_topology(self, alert):
        return {"services": []}


class _FakeIncidentService:
    def get_related_incidents(self, alert):
        return []


def test_fetch_context_supports_new_context_types():
    fetcher = ContextFetcher(_FakeZabbixClient(), _FakeGraphClient(), _FakeIncidentService())

    context = fetcher.fetch_context(
        ["memory_summary", "disk_io_summary", "availability_summary", "alert_history"],
        {"status": "problem", "event_time": "2026-04-01T10:00:00"},
    )

    assert context["memory_summary"]["memory_used_percent"] == 92
    assert context["disk_io_summary"]["utilization_percent"] == 93
    assert context["availability_summary"]["ping_status"] == "down"
    assert context["alert_history"]["resolved"] is False
