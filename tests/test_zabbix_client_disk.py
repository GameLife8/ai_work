from __future__ import annotations

from services.zabbix_client import ZabbixClient


class _DiskClient(ZabbixClient):
    def __init__(self):
        super().__init__(base_url="http://example.com", use_stub=False)

    def _resolve_host_id(self, alert):
        return "host-1"

    def _get_host_items(self, host_id):
        return [
            {"itemid": "1", "key_": "vfs.fs.size[/mnt/data01,pused]", "name": "", "value_type": "0"},
            {"itemid": "2", "key_": "vfs.fs.size[/mnt/data01,used]", "name": "", "value_type": "3"},
            {"itemid": "3", "key_": "vfs.fs.size[/mnt/data01,total]", "name": "", "value_type": "3"},
        ]

    def _get_numeric_history(self, item_id, value_type, alert=None):
        history = {
            "1": [88.9, 88.91],
            "2": [1717986918400, 1954210111488],
            "3": [2197949513728, 2197949513728],
        }
        return history[item_id]


def test_get_disk_summary_can_derive_free_space_from_total_minus_used():
    client = _DiskClient()

    summary = client.get_disk_summary(
        {
            "host_name": "WYY-DB09",
            "host_ip": "169.24.7.117",
            "resource_scope": {"mount_point": "/mnt/data01"},
        }
    )

    assert summary["mount_point"] == "/mnt/data01"
    assert summary["used_percent"] == 88.91
    assert round(summary["free_gb"], 0) == 227
    assert round(summary["total_gb"], 0) == 2047
