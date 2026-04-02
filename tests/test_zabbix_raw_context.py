from __future__ import annotations

from services.zabbix_client import ZabbixClient


class _RawClient(ZabbixClient):
    def __init__(self):
        super().__init__(base_url="http://example.com", use_stub=False)

    def _get_sampled_history(self, item_id, value_type, alert):
        return [
            {
                "clock": 1712030400,
                "time": "2024-04-02T00:00:00+00:00",
                "value": 83.4,
                "source_clock": 1712030400,
                "source_time": "2024-04-02T00:00:00+00:00",
            }
        ]


def test_collect_items_returns_raw_item_shape_with_samples():
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

    result = _RawClient()._collect_items(items, ["system.cpu.util"], {"event_time": "2026-04-02 11:00:00"})

    assert result == [
        {
            "itemid": "1",
            "name": "CPU util",
            "key": "system.cpu.util",
            "value_type": "0",
            "lastvalue": "83.4",
            "units": "%",
            "samples": [
                {
                    "clock": 1712030400,
                    "time": "2024-04-02T00:00:00+00:00",
                    "value": 83.4,
                    "source_clock": 1712030400,
                    "source_time": "2024-04-02T00:00:00+00:00",
                }
            ],
        }
    ]
