"""Connection driver registry。

每种接入类型（zabbix / swarm / k8s / prometheus / ...）实现一个 Driver。
管理员在后台创建一条 Connection 时，平台根据 type 找到对应 driver，
用 driver.schema() 渲染表单、用 driver.validate() 验证连通、
用 driver.build_client() 在 skill 执行时构造 client。
"""

from ops_platform.drivers.base import ConnectionDriver, DriverField
from ops_platform.drivers.zabbix import ZabbixDriver
from ops_platform.drivers.swarm import SwarmDriver
from ops_platform.drivers.k8s import K8sDriver
from ops_platform.drivers.host_agent import HostAgentDriver
from ops_platform.drivers.alert_analysis import AlertAnalysisDriver


_DRIVERS: dict[str, ConnectionDriver] = {}


def register(driver: ConnectionDriver) -> None:
    _DRIVERS[driver.type_code] = driver


def get(type_code: str) -> ConnectionDriver:
    if type_code not in _DRIVERS:
        raise KeyError(f"未注册的接入类型：{type_code}")
    return _DRIVERS[type_code]


def all_drivers() -> list[ConnectionDriver]:
    return list(_DRIVERS.values())


def bootstrap_default_drivers() -> None:
    register(ZabbixDriver())
    register(SwarmDriver())
    register(K8sDriver())
    register(HostAgentDriver())
    register(AlertAnalysisDriver())


bootstrap_default_drivers()


__all__ = [
    "ConnectionDriver",
    "DriverField",
    "register",
    "get",
    "all_drivers",
]
