from __future__ import annotations

from services.zabbix_client import ZabbixClient


def test_find_metric_item_matches_by_prefix():
    items = [
        {"itemid": "1", "key_": "vfs.fs.size[/,free]", "value_type": "3"},
        {"itemid": "2", "key_": "system.cpu.util[,system,avg1]", "value_type": "0"},
    ]

    item = ZabbixClient._find_metric_item(items, ["system.cpu.util", "system.cpu.util[,system,avg1]"])

    assert item is not None
    assert item["itemid"] == "2"
