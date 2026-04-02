from __future__ import annotations

from services.zabbix_client import ZabbixClient


class _MemoryClient(ZabbixClient):
    def __init__(self):
        super().__init__(base_url="http://example.com", use_stub=False)

    def _resolve_host_id(self, alert):
        return "host-1"

    def _get_host_items(self, host_id):
        return [
            {"itemid": "1", "key_": "vm.memory.utilization", "name": "", "value_type": "0"},
            {"itemid": "2", "key_": "vm.memory.size[available]", "name": "", "value_type": "3"},
            {"itemid": "3", "key_": "vm.memory.size[total]", "name": "", "value_type": "3"},
        ]

    def _get_numeric_history(self, item_id, value_type):
        history = {
            "1": [80.12, 83.21],
            "2": [6442450944, 5651509248],
            "3": [33566887936, 33566887936],
        }
        return history[item_id]


def test_get_memory_summary_supports_utilization_and_available_keys():
    client = _MemoryClient()

    summary = client.get_memory_summary({"host_name": "P-L-TIDB09", "host_ip": "169.24.7.26"})

    assert summary["memory_used_percent"] == 83.21
    assert round(summary["available_gb"], 2) == 5.26
    assert round(summary["total_gb"], 2) == 31.26


def test_resolve_host_id_prefers_explicit_host_id():
    client = ZabbixClient(base_url="http://example.com", use_stub=False)

    assert client._resolve_host_id({"host_id": "12345"}) == "12345"
