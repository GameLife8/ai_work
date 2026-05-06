from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver, DriverField
from services.zabbix_client import ZabbixClient


class ZabbixDriver(ConnectionDriver):
    type_code = "zabbix"
    display_name = "Zabbix 监控"
    category = "monitoring"

    def __init__(self) -> None:
        self.fields = [
            DriverField("base_url", "API URL", required=True,
                        placeholder="http://host/zabbix/api_jsonrpc.php"),
            DriverField("username", "用户名"),
            DriverField("password", "密码", type="password"),
            DriverField("timeout_seconds", "超时(秒)", type="integer", default=10),
            DriverField("use_stub", "Stub 模式", type="boolean", default=False,
                        help="本地无 Zabbix 时勾上以便联调"),
        ]

    def build_client(self, config: dict[str, Any]) -> ZabbixClient:
        return ZabbixClient(
            base_url=config["base_url"],
            username=config.get("username", ""),
            password=config.get("password", ""),
            timeout_seconds=int(config.get("timeout_seconds") or 10),
            use_stub=bool(config.get("use_stub", False)),
        )
