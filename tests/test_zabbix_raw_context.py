from __future__ import annotations

from services.zabbix_client import ZabbixClient


def test_collect_items_returns_raw_item_shape():
    items = [
        {
            "itemid": "1",
            "name": "CPU util",
            "key_": "system.cpu.util",
            "value_type": "0",
            "lastvalue": "83.4",
            "units": "%",
        },
        {
            "itemid": "2",
            "name": "Memory util",
            "key_": "vm.memory.util",
            "value_type": "0",
            "lastvalue": "66",
            "units": "%",
        },
    ]

    result = ZabbixClient._collect_items(items, ["system.cpu.util"])

    assert result == [
        {
            "itemid": "1",
            "name": "CPU util",
            "key": "system.cpu.util",
            "value_type": "0",
            "lastvalue": "83.4",
            "units": "%",
        }
    ]
